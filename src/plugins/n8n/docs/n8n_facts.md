# n8n facts: test instance n8n 2.39.9 (Community, Docker, SQLite)

As of 2026-09-21, after the switch to the instance MCP and review round 2. This document is the basis on which `docs/design.md` rests. **Check before building.** Example URLs use `http://localhost:5678`; `<base>` stands for the instance's base URL.

**How facts are labeled**
- **[measured]**: an explorer executed it and saw the result.
- **[documented]**: spec, source code or docs; the source is given.
- **[assumed]**: unchecked.
- **SUPERSEDED**: the statement is no longer true or no longer supports the design. The replacement is named, if there is one; the ID stays so that old references remain resolvable.
- **no longer load-bearing**: the statement is still true, but since the switch to the instance MCP (design §2) no part of the design builds on it any more. It is listed under "rejected, because" (design §13).

**Sources**
- Probe scripts and raw data lie in the scratchpad of the measurement runs:
  - `work/p1.py`–`p20.py`: API map,
  - `work/nt/`: nodes map,
  - `work/connect/`: bridge map,
  - `work/probe_*.py`: our side,
  - `work/rv/m1.py`–`m3.py`, `rv_lic.py`: review round 1,
  - `mcp_probe.py`, `validate_cases.py`: instance MCP (main session),
  - `work2/t1*.py`–`t9b.py`: instance MCP measurement,
  - `work2/r1_*`–`r6.py`: review round 2,
  - `smoke.py`, `m7_m9.py`, `newcred.py`: build of phase 1a,
  - `review/*.py`: review round 3 (big review), `fix/*.py`: follow-up measurements for it.
- `openapi.yml` is the instance's spec (info.version 1.1.1).
- `work/paths.txt` is the `/discover` output (endpoint → scope).
- The markers:
  - "(follow-up measurement)": re-checked on 2026-09-21,
  - "(review)": measured in the review and run again afterwards,
  - "(review 2)": measured in review round 2,
  - "(main session)": measured by the main session.
- No key and no password ended up in script outputs or files.

## Instance
- **F-INST1 [measured]** n8n 2.39.9 with a Community license and SQLite. `executionMode: regular`, the public API is active. Source: `/rest/settings` `versionCli`, `/rest/license` planName Community.
- **F-INST2 [measured]** `/healthz` and `/healthz/readiness` return 200. `/metrics` returns 404 [assumed: the reason is `N8N_METRICS`].
- **F-INST3 [measured, follow-up measurement]** State after all measurement runs: 0 workflows, 0 tags, 0 `zz-probe-*`. Sources:
  - `GET /api/v1/workflows?limit=250`, `/tags` and `/credentials`,
  - the cleanup steps of t1–t7 [M-MCP-28], t8/t9 [M-MCP-37] and r2–r6 [M-MCP-45].
- **F-INST4 [measured, review]** Instance settings:
  - `excludeNodes` = executeCommand, localFileTrigger, e2eTest, dynamicCredentialCheck.
  - `security.blockFileAccessToN8nFiles: true`. By its name this protects only the n8n directory, not the rest of the file system [documented: setting name; the reach is assumed].

  Source: `/rest/settings` (rv_lic.py, settings_auth.json).

## Deploy
- **F-DEP1 [measured, follow-up measurement]** In `src/plugins/n8n/docs/deploy/docker-compose.yml` there is:
  - line 7: `restart: unless-stopped`.
  - line 15: `N8N_SECURE_COOKIE` with default `true`.
  - line 18: `N8N_RUNNERS_ENABLED: "true"` (task runner for Code nodes; see [M-MCP-38]).
  - lines 21–22: `N8N_MCP_MANAGED_BY_ENV: "true"` and `N8N_MCP_ACCESS_ENABLED: "true"` [M-MCP-22].

  On the test instance the `.env` sets `N8N_SECURE_COOKIE=false`, because it is reached over HTTP across the network; hence the cookie without `Secure` [F-EMB1].
- **F-DEP2 SUPERSEDED (dropped)**
- **F-DEP3 SUPERSEDED (dropped)** What remains measured: the compose file sets no memory limit.
- **F-DEP4 SUPERSEDED → F-DEP5** `.env` and `CREDENTIALS` were not git-ignored.
- **F-DEP5 [measured, 2026-09-21]** `src/plugins/n8n/docs/deploy/.gitignore` contains `.env` and `CREDENTIALS`. `git check-ignore -v` returns rc=0 for both and a match line from this file.
- **F-DEP6 [measured, main session]** State of the deploy files (phase 0 from design §11 implemented):
  - `docs/deploy/` contains `docker-compose.yml` (image `docker.n8n.io/n8nio/n8n:2.39.9`, without comments, with the MCP variables [F-DEP1]), `.env.example`, `setup_owner.sh`, `.gitignore` and `README.md`.
  - `.env.example` contains `N8N_ENCRYPTION_KEY`, `N8N_PUBLIC_URL`, `N8N_PORT`, `GENERIC_TIMEZONE` and `N8N_SECURE_COOKIE`.
  - `setup_owner.sh` runs each step individually and repeatably: create owner (400 if one already exists), log in, create a public API key with the **four read-only scopes** `workflow:read`, `workflow:list`, `execution:read`, `execution:list`, check that the instance MCP is on (otherwise abort with a pointer to the compose variables), fetch the MCP key via `POST /rest/mcp/api-key/rotate`. Each step is skipped if its value is already in `CREDENTIALS`. Keys are never printed.
  - Run on the test instance: owner present, key created, MCP on, MCP key rotated; the old full key [F-AUTH4] is deleted.
- **F-DEP7 [measured, main session]** Without `N8N_HOST`, `N8N_PORT` and `N8N_PROTOCOL`, n8n derives its URLs from `N8N_EDITOR_BASE_URL` and `WEBHOOK_URL`: `/rest/settings` shows `urlBaseWebhook` and `urlBaseEditor` equal to `N8N_PUBLIC_URL`. Owner login, API key and MCP key survived recreating the container with the generic compose file, because `N8N_ENCRYPTION_KEY` stayed the same.

## Auth (public API)
- **F-AUTH1 [measured]** The public API takes the key in the header `X-N8N-API-KEY`. Bearer with the API key gives 401. Source: p3.py.
- **F-AUTH2 [measured]** A missing or wrong key gives 401 `{"message":"Unauthorized"}`. Even after 100 failed attempts there is no lockout. Source: p15.py.
- **F-AUTH3 [measured]** Scoped keys work. If a scope is missing, 403 `{"message":"Forbidden"}` comes back without `reason`. `/discover` shows only the permitted endpoints. Source: p16.py, with a temporary key that was deleted afterwards.
- **F-AUTH4 SUPERSEDED → F-AUTH8** The first key of the test instance had all 105 owner scopes, including `user:*`, LDAP, SAML and `communityPackage:install`, because the first version of `setup_owner.sh` requested all scopes. The script is corrected [F-DEP6], the key deleted. Source: p2.py `/discover`, `/rest/api-keys/scopes`.
- **F-AUTH5 [measured]** The public API has no rate limit: 300 GETs with 10 threads gave only 200s, there are no rate-limit headers, and the spec knows no 429. Source: p15.py, grep in openapi.yml. The counterpart is the instance MCP [M-MCP-24].
- **F-AUTH6 [documented]** Scope per endpoint (excerpt):

  | Endpoint | Scope |
  |---|---|
  | `GET /workflows` | `workflow:list` |
  | `GET /workflows/{id}` | `workflow:read` |
  | `PUT /workflows/{id}` | `workflow:update` |
  | `POST /workflows/{id}/publish` and `/activate` | `workflow:activate` |
  | `/unpublish` and `/deactivate` | `workflow:deactivate` |
  | **`POST /workflows/{id}/archive` and `/unarchive`** | **`workflow:delete`** |
  | `GET /executions` | `execution:list` |
  | `GET /executions/{id}` | `execution:read` |
  | `GET /workflows/{id}/tags` | `workflowTags:list` |
  | `POST /tags` | `tag:create` |

  Source: `work/paths.txt` (`/discover`).
- **F-AUTH7 [documented]** Further details from the spec:
  - `GET /workflows/{id}` returns in the schema among others `tags`, `isArchived`, `versionId`, `activeVersionId`, `activeVersion`, `settings` and `nodes`.
  - `GET /workflows` knows the query parameters `tags`, `name`, `active`, `projectId`, `limit`, `cursor` and `excludePinnedData`.
  - `GET /executions` knows `workflowId`, `status`, `startedAfter`, `startedBefore`, `includeData`, `limit` and `cursor`.

  Source: openapi.yml, via yaml.safe_load on 2026-09-21.
- **F-AUTH8 [measured, main session]** The key with the four read-only scopes [F-DEP6]:
  - `GET /workflows` and `GET /executions` return 200; `POST /workflows` and `GET /users` return 403.
  - `GET /workflows/{id}` contains `tags`, `isArchived` and `settings.availableInMCP`; `GET /workflows?tags=<name>` filters correctly (answers M12). No further scope is needed for the bolt.

  Source: m12.py (workflow created and tagged via MCP, read with the read key, then archived and deleted; 0 left).

## Public API in general
- **F-API1 [documented]** The spec has 134 operations. None of them:
  - executes a workflow,
  - executes a single node,
  - validates without saving.

  Something can be executed only via:
  - the production webhook,
  - `POST /executions/{id}/retry`,
  - test-runs (license-locked).

  Source: openapi.yml, grep `x-eov-operation-id`. The instance MCP fills the gap [M-MCP-3].
- **F-API2 [measured]** Body of write requests:
  - Required fields are `name`, `nodes`, `connections` and `settings`; `settings: {}` suffices.
  - Read-only fields (`id`, `active`, `versionId` …) and unknown keys give 400.
  - POST returns 200, not 201.

  Source: p5.py.
- **F-API3 [measured]** Pagination is by cursor: `{data, nextCursor}`. The cursor is base64-encoded JSON `{lastId, limit}`; an invalid cursor gives 400. Source: p12.py.
- **F-API4 [assumed]** Whether `limit>250` is capped is unchecked. Measured is only that 300 and 999 give no 400.

## PUT and versions (public API) — no longer load-bearing
The plugin no longer writes via the public API (design §2, §13). The following facts remain valid; they become important if someone writes via PUT after all.
- **F-PUT1 [measured]** A GET body cannot be written back unchanged via PUT, because `id` and `versionId` are read-only (400). Source: p8.py.
- **F-PUT2 [measured]** With `description:null` (as GET returns it) PUT answers with 400. `staticData:null` and `pinData:null` are accepted. Writable are:
  - `name, nodes, connections, settings, staticData, pinData, nodeGroups`,
  - `description` (PUT only),
  - for POST additionally `projectId` and `parentFolderId`.

  Source: p8.py; openapi.yml.
- **F-PUT3 [measured]** There is no optimistic locking: `versionId` in the body gives 400 (read-only), and the spec knows no If-Match. Source: p8.py.
- **F-PUT4 [measured]** PUT with broken content onto an **active** workflow (default `publishIfActive=true`) returns 400. The draft is **saved anyway** and gets a new versionId; the old version stays live. Source: p14.py.
- **F-PUT5 [measured]** The two version fields:
  - `versionId` is the draft, `activeVersionId` the live state.
  - `PUT ?publishIfActive=false` saves only a draft.
  - A standard PUT publishes immediately.
  - An identical PUT creates no new version.
  - `publish {versionId}` rolls back; an unknown version gives 404.

  Source: p9.py.
- **F-PUT6 [assumed]** How `publishIfActive=false` behaves with broken content on an active workflow is unmeasured.
- **F-PUT7 [documented]** According to the spec, the automatic republishing via PUT has two consequences:
  - It additionally needs `workflow:activate`.
  - If the scope is missing, the state is saved as a draft anyway, with 403 `reason: insufficient_api_key_scope`.

  Source: `work/wf_ops.txt` (PUT description).

## Validation by the server (public API)
- **F-VAL1 [measured]** On create or PUT the server rejects structural errors: 400 "Workflow structure is invalid". This concerns:
  - `unknown_connection_target/source`,
  - `duplicate_node_name`,
  - missing type, name or position,
  - wrong shape of the connections.

  Source: p13.py.
- **F-VAL2 [measured]** The following is silently stored on create and rejected with 400 **only at publish**:
  - an unknown node type,
  - a wrong typeVersion on non-triggers,
  - a missing required parameter,
  - a missing required credential (`Missing required credential: slackApi`),
  - a missing trigger.

  Source: p13.py, nt/p5-p6.
- **F-VAL3 [measured]** The following gets through **all** checks of the public API and fails only at runtime or silently:
  - webhook typeVersion 99 or 9.9,
  - empty webhook path (404 afterwards),
  - `httpMethod:"FROB"`,
  - `method:"FOO"`,
  - an unknown parameter,
  - a non-existent credential ID,
  - `responseMode:responseNode` without a Respond node (runtime 500),
  - AI Agent without a language model,
  - an incomplete expression `={{ …` (runs).

  Source: p13.py, nt/p5-p6. How the instance MCP's validators handle this is in [M-MCP-H8][M-MCP-H9][M-MCP-8]–[M-MCP-11].
- **F-VAL4 [measured]** If the webhook path collides with an active workflow, 409 comes back at publish. Source: p14.py.
- **F-VAL5 [documented]** The server check consists of `validateNodeCredentials`, `NodeHelpers.getNodeParametersIssues` and the trigger check. It skips deactivated nodes and unconnected non-triggers. Source: n8n@2.39.9 `dist/workflows/workflow-validation.service.js:32-117`.
- **F-VAL6 [measured] no longer load-bearing** An offline rebuild with `n8n-workflow@2.39.3` hits all server results, **but only if the defaults are filled in beforehand**. Source: nt/side/val2.js, dbg.js.
- **F-VAL7 [measured] no longer load-bearing** Control test with the templates 1954, 3050 and 2465:
  - `n8n-workflow` finds 0 findings.
  - The SDK finds 0, 0 and 2 warnings.

  Source: side/val3.js.
- **F-VAL8 [documented] no longer load-bearing** n8n 2.39.9 depends on `n8n-workflow` 2.39.3 and `@n8n/workflow-sdk` 0.32.3, both under the Sustainable Use License. An npm install takes 57 s and 145 MB [measured]. Source: registry.npmjs.org, LICENSE.md.

## Test run via the internal API `/rest` — no longer load-bearing
The test run now goes via `test_workflow` of the instance MCP [M-MCP-3]–[M-MCP-6]. The following facts explain why the former `/rest` path is rejected (design §13).
- **F-RUN1 [measured]** `POST /rest/workflows/{id}/run` with a session cookie executes **only the saved** draft:
  - Payload pinData and payload parameters have no effect.
  - Saved pinData takes effect, also on non-trigger nodes.

  Source: p19.py, p20.py, rv/m1.py (executions 95–104).
- **F-RUN2 [measured]** Nodes that exist only in the payload are ignored by the server; e.g. 500 `Could not find a node named …` comes back. Source: p20.py.
- **F-RUN3 [measured]** Without a trigger and without `destinationNode`, 400 comes back. Source: p20.py.
- **F-RUN4 SUPERSEDED → M5/M7 in design §10.3** The open points on schedule triggers and AI subnodes now apply to `test_workflow`.
- **F-RUN5 [measured, review]** A webhook trigger without pinData returns `{"data":{"waitingForWebhook":true}}`; no execution is created. Source: rv/m3.py.
- **F-RUN6 [measured, review]** A PUT that sets only pinData does not change the `versionId`. Source: rv/m2.py.

## Node catalog `/types/*` — no longer load-bearing
Node knowledge now comes from `search_nodes` and `get_node_types` of the instance MCP [M-MCP-25][M-MCP-32]. The following facts remain valid.
- **F-NOD1 [measured]** `/types/nodes.json` and `/types/credentials.json` return 401 without auth **and** with an API key. 200 only with the session cookie. Source: nt/p1.py; `dist/server.js:374-385`.
- **F-NOD2 [measured, follow-up measurement]** `/types/node-versions.json` likewise returns 401 with an API key.
- **F-NOD3 [measured]** nodes.json:
  - has 17,743,676 bytes, 990 entries and 912 names,
  - contains 1,375 `displayOptions` `@version` conditions with `_cnd` operators.

  Source: a1.py, a4.py.
- **F-NOD4 [measured; the hint's statement refuted by M-MCP-38]** 161 nodes have `builderHint`, 193 properties have `propertyHint`. The Code node says e.g. "LAST RESORT … sandbox has NO network access". Source: a5.py. **Correction:** measured, this is true only for `fetch` (undefined); via `this.helpers.httpRequest` Code has network access [M-MCP-38]. The hint thus proves only its own text, not the isolation.
- **F-NOD5 [measured]** 4,306 properties use `loadOptionsMethod`, plus 1,659 resourceLocators. Source: a4.py.
- **F-NOD6 [measured]** A conditional GET returns 304; the ETag has the form `W/"size-mtime"`. Source: p2.py.
- **F-NOD7 [measured]** A search index with one line per node costs ~13.1k tokens for 512 nodes and ~26.2k tokens for all 912. Source: idx.py.
- **F-NOD8 [measured]** Compact details cost 0.45k–2.4k tokens. Show and hide must not be merged (`sheetName`). Source: compact.py.
- **F-NOD9 [measured]** The trigger flag "has webhooks" is wrong; correct is `group ∋ trigger`. Source: idx.py.
- **F-NOD10 [measured]** `executeCommand` and `localFileTrigger` are missing from nodes.json, because `excludeNodes` excludes them [F-INST4].
- **F-NOD11 [measured, review]** 54 entries are `hidden: true` and still usable in the JSON. Among them are some with file or code effect:
  - `n8n-nodes-base.readBinaryFile`, `readBinaryFiles`, `writeBinaryFile`,
  - `function`, `functionItem`,
  - `@n8n/n8n-nodes-langchain.code`, `@n8n/n8n-nodes-langchain.toolHttpRequest`.

  **Still load-bearing** for the block list (design §8.2). Source: work/nodes.json.
- **F-NOD12 [measured, review]** For the webhook, `httpMethod` defaults to `GET`; with `multipleMethods=true` the default is `["GET","POST"]`. **Still load-bearing** for `n8n_trigger_workflow`. Source: work/nodes.json.
- **F-NOD13 [measured, review]** These nodes have outward effect **without** a credential:
  - `n8n-nodes-base.executeWorkflow` and `@n8n/n8n-nodes-langchain.toolWorkflow` (start other workflows),
  - `rssFeedRead`,
  - `toolHttpRequest`,
  - `git` (credential optional),
  - `graphql` (credential depending on `authentication`).

  **Still load-bearing** for the pinning rule (design §3.3). Source: work/nodes.json.

## Credentials
- **F-CRED1 [measured]** Secrets are not readable through any API. Source: nt/p4.py.
- **F-CRED2 [measured]** `GET /credentials/schema/{type}` returns a JSON schema. Source: p3.py, p4.py.
- **F-CRED3 [measured]** In a workflow a credential appears as `node.credentials[<credType>] = {id, name}`. At publish the server checks only that key and `id` are present, not that the ID exists. Source: openapi.yml:12049; nt/p6. The same holds for the instance MCP [M-MCP-10][M-MCP-11].
- **F-CRED4 [assumed]** OAuth2 (74 types) needs a browser for the consent.

## Executions
- **F-EXE1 [measured]** `GET /executions/{id}?includeData=true` contains `data.resultData{runData, lastNodeExecuted, error}`. Source: p7.py.
- **F-EXE2 [documented/measured]** The status values are canceled, crashed, error, new, running, success, **unknown** and waiting. Source: openapi.yml; p12.py. The same enum is in the outputSchema of `test_workflow` [M-MCP-H6].
- **F-EXE3 [measured]** Retry:
  - Retry of a successful execution gives 409.
  - With `loadWorkflow:true` 500 comes back after a fix.
  - A failing webhook workflow answers with 500.

  Source: p10.py, p11.py.
- **F-EXE4 [measured]** Pruning: maxAge 336 h, maxCount 10,000. Source: `/rest/settings`. A 404 on an execution can also come from a storage setting of the workflow [M-MCP-41].

## Lifecycle
- **F-LIFE1 [measured]** `DELETE` on an active workflow returns 200. **All executions are deleted along with it.** Source: p17.py.
- **F-LIFE2 [measured]** Archiving:
  - `archive` takes back the publication.
  - PUT and publish on an archived workflow give 400.
  - `unarchive` returns 200.

  Source: p18.py.
- **F-LIFE3 [measured]** `unpublish`/`deactivate` are idempotent. Source: p18.py.

## Tags
- **F-TAG1 [measured]** Tags via the public API:
  - `POST /tags` returns 201.
  - `PUT /workflows/{id}/tags` expects `[{"id":…}]`.
  - The name filter is a partial match.

  Source: p12.py; confirmed by [M-MCP-16].

## Error shapes (public API)
- **F-ERR1 [measured]** 400 has the form `{"message":"request/<place>/<field> <reason>"}`. Source: p5.py.
- **F-ERR2 [measured]** An unknown path gives 404, a wrong content type 415. Source: p3.py.
- **F-ERR3 [measured]** Broken JSON in the request gives **500 with an HTML body**.

## License
- **F-LIC1 [measured]** Locked by license (403) are variables, projects, folders, sourceControl, test-runs and logStreaming. Source: p3.py, p12.py, p17.py.
- **F-LIC2 [measured, review]** From `/rest/settings`:
  - `advancedPermissions: false`, `customRoles: false`, `sharing: false`, `projects.team.limit: 0`. So there is only an owner and members, but no admin role.
  - `communityNodesEnabled: true`, `unverifiedCommunityNodesEnabled: true`.
  - `mfa.enabled: true`, `enforced: false`.

  Source: rv_lic.py.

## Bridge
- **F-BR1 [measured]** n8n reaches other hosts in its network via HTTP Request. `User-Agent: n8n` and custom headers are sent along. Source: connect, execution 75.
- **F-BR2 [measured]** In expressions `$execution.id`, `$workflow.id` and `$execution.resumeUrl` are available. Source: connect.
- **F-BR3 [measured]** URL forms:
  - webhook: `<base>/webhook/<path>`,
  - test: `/webhook-test/<path>`,
  - wait: `/webhook-waiting/<execId>`.

  Source: `/rest/settings`; p6.py.
- **F-BR4 [measured]** The production webhook's response contains **no** execution ID. If the workflow is not published, 404 "not registered" comes back. Source: p6.py.
- **F-BR5 [measured]** Webhook plus Respond to Webhook returns its own JSON, including `executionId`. Source: connect.
- **F-BR6 [measured]** `settings.errorWorkflow` fires **only if the error workflow is published**. The payload contains `execution{id,url,error,lastNodeExecuted,mode}` and `workflow{id,name}`. Source: connect, executions 77–80. The instance MCP enforces this on setting [M-MCP-15].
- **F-BR7 [measured]** The wait `resumeUrl` is HMAC-signed: without `signature` 401, with signature 200. While the execution is waiting, `status: waiting` is shown. Source: connect, execution 76.
- **F-BR8 [measured]** The HTTP Request node without a `timeout` option aborts after 300 s. Source: connect, executions 90/91.
- **F-BR9 [measured]** `lmChatOpenAi` v1.3 has `timeout` 60,000 ms and `maxRetries` 2 by default. Source: connect `cx_llm.py`.

## MCP server trigger (workflow as MCP server)
- **F-MCP1 [measured]** Our `mcp_client` (`transport: streaming`) talks to an n8n MCP server trigger v2.x at `<base>/mcp/<path>`:
  - Connecting, listing and calling work.
  - The tool name is the node name.
  - Every call creates an execution.
  - Bearer auth: a wrong token gives 403.

  Source: probe_mcp_trigger.py.
- **F-MCP2 [measured]** `transport: sse` returns 404. Source: probe_mcp_trigger.py.
- **F-MCP3 [documented]** Remote servers are declared in `config/mcp_servers.yaml` under `external_servers.remote_servers`. Source: `mcp_servers.yaml:26-31`; `config/models/external_servers.py`.
- **F-MCP4 SUPERSEDED → M-MCP-H1…, M-MCP-1…** It used to say: "instance MCP off, tools unmeasured." Now it is switched on and measured. What remains valid: if the MCP is off, the endpoint answers 404 "MCP access is disabled".
- **F-MCP5 SUPERSEDED → M-MCP-H2** The probe key from back then is rotated. We now know the endpoint for it.

## Instance MCP `/mcp-server/http` — main session
- **M-MCP-H1 [measured, main session]** Switching on via `PATCH /rest/mcp/settings {"mcpAccessEnabled": true}` with an owner session returns 200 `{"mcpAccessEnabled":true,"autoExposeNewWorkflows":false}`.
  - The state is in `GET /rest/module-settings` → `mcp`.
  - `GET /rest/mcp/settings` gives 404.
  - On the test instance the MCP is switched on.

  The route via env variables is described by [M-MCP-22]; the design uses it instead of the PATCH.
- **M-MCP-H2 [measured, main session]** The MCP key:
  - `GET /rest/mcp/api-key` returns it **only masked** (10 characters) once it exists.
  - `POST /rest/mcp/api-key/rotate` returns 200 with a new **raw key** (272 characters); the old key is invalid afterwards [assumed: rotation means replacing].
  - The key object has the fields apiKey, audience, createdAt, id, label, lastUsedAt, scopes, updatedAt and userId.
- **M-MCP-H3 [measured, main session]** The endpoint `<base>/mcp-server/http`:
  - speaks streamable HTTP with JSON-RPC,
  - requires `Authorization: Bearer <MCP key>`,
  - identifies itself as `{"name":"n8n MCP Server","version":"1.1.0"}`,
  - uses protocol 2025-06-18.

  Responses come as JSON or SSE. **Corrected → M-MCP-54:** 2.39.9 does not send a session ID in the header `mcp-session-id`; the server is stateless.
- **M-MCP-H4 [measured, main session]** The server has 35 tools:
  - search_workflows, execute_workflow, get_workflow_execution, search_workflow_executions, get_workflow_details,
  - get_workflow_history, get_workflow_version, get_workflow_versions_diff,
  - publish_workflow, unpublish_workflow,
  - prepare_workflow_pin_data, test_workflow,
  - list_credentials, list_n8n_gateway_services, list_workflow_tags,
  - search_data_tables, create_data_table, rename_data_table, add_data_table_column, delete_data_table_column, rename_data_table_column, add_data_table_rows, get_data_table_rows,
  - search_nodes, get_node_types, get_workflow_best_practices, explore_node_resources,
  - validate_workflow, validate_node_config, create_workflow_from_code,
  - search_projects, archive_workflow, update_workflow, restore_workflow_version, get_workflow_sdk_reference.

  There is no delete tool.
- **M-MCP-H5 [measured, main session]** Building is done in SDK code, not in JSON:
  - `import { workflow, node, trigger } from '@n8n/workflow-sdk'`,
  - `create_workflow_from_code(code, skillsUsed, name, description, versionName, versionDescription, projectId, folderId)`,
  - `update_workflow(workflowId, skillsUsed, operations, versionName, versionDescription)` with atomic operations.
- **M-MCP-H6 [measured, main session; partly SUPERSEDED by M-MCP-30]** Signature: `test_workflow(workflowId, pinData REQUIRED, triggerNodeName?, timeout ≤3600, default 300)`.
  - Items must be in `{"json":…}`.
  - The tool description claims that trigger, credential and HTTP Request nodes would be pinned; Set, If, Code and credential-free I/O nodes would run normally.
  - According to the description `timeout` interrupts the test execution [documented, tool description].
  - The outputSchema status enum is the same as in F-EXE2 [measured, t8.py].
- **M-MCP-H7 [measured, main session; size corrected by M-MCP-26]** `search_nodes` for 2 search terms: 12,155 characters. The main session gives the SDK reference as 101,547 characters; that is a JSON dump [M-MCP-26].
- **M-MCP-H8 [measured, main session]** `validate_node_config` ("schema-level only"):
  - **Detected:** webhook `httpMethod "FROB"`, httpRequest `method "FOO"`, Slack message/post without `text`.
  - **Overlooked (valid):** webhook typeVersion 99, Set with an unknown parameter, empty webhook path, unknown node type.
- **M-MCP-H9 [measured, main session]** `validate_workflow(code)`:
  - **Detected:** unknown node type ("Unrecognized node type").
  - **Overlooked (valid):** webhook version 99, empty webhook path.
  - Control case: `{"valid":true,"nodeCount":2}`.

## Instance MCP — measurement of 21.09. (work2/t1–t7)
- **M-MCP-1 [measured]** `create_workflow_from_code` returns `{workflowId, name, nodeCount, url, autoAssignedCredentials[], targetProject{id,name,type}}`.
  - Optionally targetFolder, note, skippedGroups, hint and warnings are added.
  - The content is there twice, in `content[0].text` and in `structuredContent`.
  - `url` has the form `<base>/workflow/<id>`.
  - `targetProject.type` is `personal`, the key owner's personal project.

  Source: t1.py; outputSchema from tools/list.
- **M-MCP-2 [measured]** `prepare_workflow_pin_data` returns `{nodeSchemasToGenerate, nodesWithoutSchema, nodesSkipped, coverage}`: only schemas, never pinData itself.
  - Fresh workflow manual→Set→Code→NoOp: `nodesWithoutSchema=['Start']`, `nodesSkipped=['SetVals','Marker','End']`.
  - Webhook+Slack: `nodesWithoutSchema=['Hook','Slack']`.
  - Whether schemas come after earlier executions is unmeasured (measurement point M13).

  Source: t1b.py, t2b.py.
- **M-MCP-3 [measured]** The caller's pinData on Set and Code nodes is honored.
  - With pins on Set and Code the live marker `CODE_LIVE_MARKER` disappears; the pinned value `CODE_PINNED` is output.
  - Downstream nodes get the pinned data.
  - HTTP Request: see [M-MCP-39]. Other types are unmeasured.

  Source: execution 105 (pins as prepared: marker live) versus 106 (pins on SetVals and Marker).
- **M-MCP-4 [measured]** `test_workflow` returns only `{executionId, status[, error]}` (157 characters including the wrapper), no node results.
  - The execution is stored in mode `manual` (provided the storage settings allow it [M-MCP-41]).
  - It is readable via the public API `GET /api/v1/executions/{id}?includeData=true` (200, ~3.4 KB with runData per node).
  - Likewise via MCP `get_workflow_execution`: 242 characters of metadata, with `includeData=true` 2,095 characters, filterable via `nodeNames`/`truncateData`.

  Source: t1b.py, t1c.py.
- **M-MCP-5 [measured]** `test_workflow` executes the **unpublished draft**.
  - This works on a never-published workflow (`activeVersionId` null).
  - On a published workflow with a later-changed draft, the test ran the draft (`V2_DRAFT`), the production webhook, in contrast, the published version (`V1`).

  Source: t1c.py, executions 108/109.
- **M-MCP-6 [measured]** Error shapes:
  - A failing node gives `{executionId:'107', status:'error', error:'PROBE_BOOM [line 1]'}`: only the message, without node name and **without** MCP `isError`. The `executionId` is set; this distinguishes a failed test result from a tool error.
  - `test_workflow`, `execute_workflow` and `publish_workflow` on a workflow that is not released return a normal result with `status`/`success=false` plus `error`, without `isError`.
  - `get_workflow_details`, `update_workflow`, `archive_workflow`, `prepare_workflow_pin_data` and `get_workflow_history` set `isError=true`.

  Source: t1c.py, t4.py.
- **M-MCP-7 [measured]** Return values of publish and unpublish:
  - `publish_workflow` returns `{success:true, workflowId, activeVersionId}`, `unpublish_workflow` returns `{success:true, workflowId}`.
  - Also successfully published was a webhook workflow with `responseMode responseNode` without a Respond node, with a ghost credential and with an unbalanced expression.

  Source: t1c.py, t2b.py.
- **M-MCP-8 [measured]** `validate_workflow` reports `valid:true` even if its warnings describe real errors. Findings are only in `warnings[] {code,message,nodeName}`:
  - AI Agent without a model subnode: `valid:true` + `MISSING_REQUIRED_INPUT` (+ `AGENT_STATIC_PROMPT`, `AGENT_NO_SYSTEM_MESSAGE`).
  - Set assignments as a string: `valid:true` + `SET_INVALID_ASSIGNMENT` + `INVALID_PARAMETER`.
  - `includeOtherFields:'yes'`: `valid:true` + `INVALID_PARAMETER`.

  Source: t2.py.
- **M-MCP-9 [measured]** `validate_node_config`:
  - **Detected (valid:false):** AI Agent without subnodes (path `subnodes`), Set assignments with a wrong type, Set `includeOtherFields` with a wrong type.
  - **Overlooked (valid:true):** credential with a non-existent ID (the tool has no credential field at all), unbalanced expression `={{ $json.a`, number assignment with value `'abc'`, webhook `responseMode responseNode`.

  Source: t2.py.
- **M-MCP-10 [measured]** `validate_workflow` **overlooked** the following cases; all gave `valid:true` without a warning:
  - credential reference with a non-existent ID,
  - wrong credential type key (`githubApi` on Slack),
  - webhook `responseNode` without a Respond node,
  - unbalanced expression `={{ $json.a`,
  - Set number assignment with text.

  Source: t2.py.
- **M-MCP-11 [measured]** Consequences at runtime:
  - `create_workflow_from_code` stores a non-existent credential ID unchanged (`{slackApi:{id:'doesNotExist999',name:'Ghost'}}`), without a warning.
  - The unbalanced expression `={{ $json.a` silently gave `null` at runtime (Set output `{x:null}`, status success).

  Source: t2b.py; execution 110.
- **M-MCP-12 [measured]** Workflows created this way are unpublished drafts (`active:false`, `activeVersionId:null`). They have `settings {executionOrder:'v1', availableInMCP:true}` and lie, without `projectId`, in the key owner's personal project. Source: t1b.py, public GET.
- **M-MCP-13 [measured]** `update_workflow` knows these operation types:
  - updateNodeParameters, setNodeParameter (JSON pointer), addNode, removeNode, renameNode,
  - addConnection, removeConnection,
  - setNodeCredential, setNodePosition, setNodeDisabled, setNodeSettings,
  - setWorkflowMetadata, setWorkflowSettings,
  - addTags, removeTags, setNodeGroups.

  Further properties:
  - At most 100 operations per call, atomic.
  - `setWorkflowSettings` covers errorWorkflow, timezone, executionOrder, save* flags (saveManualExecutions, saveData*Execution, saveExecutionProgress), executionTimeout, timeSavedPerExecution, callerPolicy and callerIds, but not `availableInMCP`.
  - The result contains `validationWarnings` (with `preExisting`).

  Source: inputSchema/outputSchema from tools/list (schemas.py, r1_tools.json).
- **M-MCP-14 [measured]** Tags work via MCP: `update_workflow addTags` with an unknown name creates the tag and attaches it. It is visible via public `GET /workflows/{id}/tags`, `list_workflow_tags` and in the tag filter of `search_workflows`. `create_workflow_from_code` has no tag field. Source: t3.py; for the field name see [M-MCP-31].
- **M-MCP-15 [measured]** `setWorkflowSettings errorWorkflow` is checked on the server.
  - **Rejected:** an unknown ID, an unpublished workflow ("has no published version") and a published workflow without an Error Trigger ("has no active Error Trigger node").
  - **Accepted:** a published workflow with `n8n-nodes-base.errorTrigger`.

  Source: t3.py, t3b.py, t3c.py.
- **M-MCP-16 [measured]** Public API:
  - `PUT /workflows/{id}/tags` needs tag IDs; the body `[{name}]` gives 400.
  - `PUT /workflows/{id}` accepts `settings.errorWorkflow='notAWorkflowId'` without a check (200) and keeps `availableInMCP`.

  Source: t3.py, t3b.py.
- **M-MCP-17 [measured]** The MCP key has:
  - `scopes = []`,
  - audience `mcp-server-api`,
  - label `MCP Server API Key`,
  - the owner as `userId` (role global:owner).

  It acts as this user. Source: t4.py.
- **M-MCP-18 [measured]** `GET /rest/module-settings` returns for `mcp` the value `{mcpAccessEnabled:true, mcpManagedByEnv:false, serverUrl:'<base>/mcp-server/http', autoExposeNewWorkflows:false}`. Source: t4.py.
- **M-MCP-19 [measured]** `search_workflows` lists **all** workflows that the key's user sees, including those not released. Every preview carries `availableInMCP`, `tags`, `triggerCount` and `parentFolderId`. Source: t4.py.
- **M-MCP-20 [measured]** There is a per-workflow release gate.
  - A workflow created via the public API has no `availableInMCP`.
  - On it `get_workflow_details`, `prepare_workflow_pin_data`, `test_workflow`, `execute_workflow`, `get_workflow_history`, `update_workflow`, `publish_workflow` and `archive_workflow` all fail: "Workflow is not available in MCP. …"

  Source: t4.py.
- **M-MCP-21 [measured]** The release also works without the UI: public `PUT /api/v1/workflows/{id}` with `settings {executionOrder:'v1', availableInMCP:true}` returns 200. Afterwards `get_workflow_details` and `test_workflow` work (execution 111). Source: t4b.py.
- **M-MCP-22 [measured, main session]** Env variables for switching on at startup are `N8N_MCP_MANAGED_BY_ENV=true` **and** `N8N_MCP_ACCESS_ENABLED=true`; both default to false and exist from 2.20.0.
  - Without `MANAGED_BY_ENV`, `ACCESS_ENABLED` is ignored.
  - With `MANAGED_BY_ENV` the value is set anew at every start, and the UI switch is read-only.

  Source: docs.n8n.io (manage-settings-using-environment-variables); source code n8n@2.39.9: `packages/@n8n/config/src/configs/instance-settings-loader.config.ts` and `packages/cli/src/instance-settings-loader/loaders/mcp-settings.loader.ts`. Measured: after recreating with both variables, `/rest/module-settings` shows `mcpAccessEnabled: true, mcpManagedByEnv: true`.
- **M-MCP-23 [documented]** For the MCP key and for `autoExposeNewWorkflows` there is no env variable in 2.39.9; the latter is a DB setting.
  - Further variables: `N8N_MCP_SERVER_RATE_LIMIT` (default 100 per IP and 5 min, 0 switches off) and `N8N_MCP_BASE_URL`.
  - `N8N_MCP_SERVER_SESSION_IDLE_TTL_MS` belongs to the MCP server trigger, not to the instance MCP.
  - For bootstrapping there are `N8N_INSTANCE_OWNER_MANAGED_BY_ENV/EMAIL/PASSWORD_HASH`.

  Source: n8n@2.39.9, `packages/cli/src/modules/mcp/mcp.config.ts`, `mcp.settings.service.ts`, `packages/@n8n/config/src/configs/mcp-server.config.ts`.
- **M-MCP-24 [measured]** Rate limit: 100 HTTP requests per IP in 5 minutes on `/mcp-server/http`.
  - After that 429 `{message:'Too many requests'}` comes back with `x-ratelimit-limit 100`, `x-ratelimit-remaining 0` and `retry-after` ~102 s.
  - Every POST counts: initialize, notifications/initialized and tools/call. A fresh session per call thus costs 3 requests.

  Source: t7.py.
- **M-MCP-25 [measured]** `get_node_types` needs objects `{nodeId, version?, resource?, operation?, mode?}`; a plain string gives `isError` (input validation). Text sizes:
  - Slack without discriminators: 644 characters, an error message with the list of resources and operations,
  - Slack message/post: 9,379,
  - Webhook: 6,833,
  - httpRequest: 17,262,
  - all three together: 24,715.

  Source: t6.py, t6b.py, t7.py.
- **M-MCP-26 [measured]** Text sizes of `get_workflow_sdk_reference` per section:

  | Section | Characters |
  |---|---|
  | patterns | 15,699 |
  | patterns_detailed | 11,905 |
  | expressions | 3,778 |
  | functions | 2,012 |
  | rules | 8,169 |
  | import | 337 |
  | guidelines | 1,678 |
  | design | 1,628 |
  | everything | 49,400 (~12k tokens) |

  The 101,547 characters from [M-MCP-H7] are the JSON dump with the doubled `structuredContent`. Source: t6b.py, t7.py.
- **M-MCP-27 [measured]** `get_workflow_best_practices`:
  - Text sizes: list 1,835, notification 5,273, chatbot 5,983 characters.
  - Enum of `technique` according to the inputSchema: **list** (overview), scheduling, chatbot, form_input, scraping_and_research, monitoring, enrichment, triage, content_generation, document_processing, data_extraction, data_analysis, data_transformation, data_persistence, notification, knowledge_base, human_in_the_loop, web_app.

  Source: t6b.py; inputSchema (r1_tools.json, review 2: `list` is in first place).
- **M-MCP-28 [measured]** Cleanup after t1–t7: 5 `zz-probe` workflows and 1 `zz-probe` tag deleted via the public API. Afterwards there were 0 workflows and 0 executions; the MCP settings are unchanged.
- **M-MCP-29 [assumed]** How the MCP key of a non-owner behaves (project visibility) is **not measured**. That would need a second user, i.e. an instance change. It is assumed that it acts with the project rights of that user.

## Instance MCP — follow-up measurement for the design rework (work2/t8–t9b, 21.09.)
- **M-MCP-30 [measured]** An **unpinned HTTP Request node runs live in `test_workflow`**.
  - Setup: manual→httpRequest `GET http://localhost:5678/healthz`, pinData only on the trigger.
  - Result: `Health` returned `{status:'ok'}` with executionStatus success (execution 112).
  - The tool description's claim that HTTP nodes would be pinned [M-MCP-H6] is thus not enforced by the server. What the caller pins is pinned.

  Source: t8.py.
- **M-MCP-31 [measured]** Tags via MCP in detail:
  - The operation is called `{"type":"addTags","names":[…]}`. With a wrong field name the result is `{"error":"Invalid operations: operation 0.names: Required"}`, without `isError`.
  - The `update_workflow` result has the keys appliedOperations, autoAssignedCredentials, name, nodeCount, url, validationWarnings and workflowId.
  - `get_workflow_details(detailLevel:'execution')` contains `workflow.tags [{id,name}]` and is 893 characters in size.
  - The keys of `workflow` are active, activeVersion, activeVersionId, canExecute, connections, createdAt, id, isArchived, meta, name, nodeCount, nodeGroups, nodes, parentFolderId, scopes, settings, tags, triggerCount, updatedAt and versionId.

  Source: t8.py, t8b.py.
- **M-MCP-32 [measured]** `get_node_types` requires `version` as a **string**; the number `2.1` gives isError (input validation).
  - An existing version (`"2.1"`) returns 7,081 characters of TypeScript definition.
  - A non-existent version (`"99"`, `"9.9"`) returns, **without isError**, the text `# Errors … Version '99' not found for node 'n8n-nodes-base.webhook'` (102 characters).

  Source: t9.py, t9b.py.
- **M-MCP-33 [measured]** A Set number assignment with value `'abc'` fails **in the test run**: `test_workflow` reports `status:error` with `"'n' expects a number but we got 'abc' [item 0]"` (execution 113). Both validators overlook the case [M-MCP-9][M-MCP-10], the test catches it. Source: t9.py.
- **M-MCP-34 [measured]** A webhook trigger with `responseMode: responseNode` without a Respond node runs **green in the test run**: pinned trigger, `status: success` (execution 114). Only the production call fails, with a runtime 500 [F-VAL3]. Validators and test thus both overlook the case. The run also proves that a webhook trigger is testable via pinData. Source: t9.py.
- **M-MCP-35 [documented]** The public API spec contradicts the measurement on `availableInMCP`. According to the spec, the workflow must be "active" and have an active webhook node for this. Measured, however, the flag is also set and effective on unpublished workflows with a manual trigger [M-MCP-12][M-MCP-21]. The measurement applies. Source: openapi.yml (`settings.availableInMCP`).
- **M-MCP-36 [measured]** The calls of t8 to t9b stayed without 429. One session per script cost 2 requests plus 1 request per tool call. Source: t8.py–t9b.py.
- **M-MCP-37 [measured]** Cleanup after t8–t9: 4 `zz-probe` workflows (live, tag, num, resp) and the tag `zz-probe-managed` are deleted (200 each). Afterwards there were 0 `zz-probe` workflows and 0 `zz-probe` tags. With the workflows, executions 112–114 are deleted [F-LIFE1]. The instance settings are unchanged.

## Instance MCP — review round 2 (work2/r1–r6, 21.09.)
- **M-MCP-38 [measured, review 2]** A **Code node has network access** via `this.helpers.httpRequest`.
  - Setup: manual→Code `Net` with `await this.helpers.httpRequest({url:'http://localhost:5678/healthz', json:true})`, only the trigger pinned.
  - Result: `Net` returned `{helpers:{status:'ok'}}` (execution 115). `fetch` is undefined in the Code node.
  - Measured with the generic compose setup (`N8N_RUNNERS_ENABLED: "true"` [F-DEP1]).
  - Refutes the builderHint's statement "sandbox has NO network access" [F-NOD4].

  Source: r2.py.
- **M-MCP-39 [measured, review 2]** pinData on an **HTTP Request node** is honored: `PinnedHttp` with URL `http://localhost:1/never` returned the pinned value `{pinned:'HTTP_PINNED'}`, status success; a live call to port 1 would have failed (execution 115). Source: r2.py.
- **M-MCP-40 [measured, review 2]** `n8n-nodes-base.executeWorkflow` v1.2 with `source: 'parameter'` executes an **inline workflow JSON**: node `Inline` with `workflowJson` = executeWorkflowTrigger→Set returned `{inline:'INLINE_RAN'}` (execution 115). According to `get_node_types(executeWorkflow)`, `source` also knows `localFile` (text contains `workflowJson`, `localFile`). The nodes in the inline JSON are not in `nodes[]` of the outer workflow. Source: r2.py.
- **M-MCP-41 [measured, review 2; narrowed → M-MCP-55]** `update_workflow setWorkflowSettings {saveManualExecutions:false, saveDataSuccessExecution:'none'}` is accepted. Afterwards `test_workflow` returns `{executionId:'117', status:'success'}`, but public `GET /executions/117?includeData=true` gives **404**: the execution is not stored. Source: r2.py; the keys are in the schema of `update_workflow` (r1_tools.json). Which of the two settings it was is separated only by M-MCP-55.
- **M-MCP-42 [measured, review 2]** Agent tool variants: `search_nodes(usage:'agentTool')` returns among others `n8n-nodes-base.httpRequestTool`, `graphqlTool`, `gitTool`, `rssFeedReadTool`, `s3Tool`, `gmailTool`, `googleSheetsTool`, `githubTool`, `gitlabTool`, `npmTool`, `@n8n/n8n-nodes-langchain.toolCode`, `toolVectorStore`, `toolSerpApi` and `@n8n/n8n-nodes-langchain.mcpRegistryClientTool`. The pattern is `<basetype>Tool` for `n8n-nodes-base`, prefix `tool…` for the langchain nodes. Source: r3.py, r4.py (run again on 21.09., same names).
- **M-MCP-43 [measured, review 2]** `get_node_types(sort)` shows `type` with the values `simple`, `random` and `code`; `merge` has the mode `combineBySql`. A Sort node with `type:'code'` ran live unpinned and changed the items (execution 119). In the Sort code, `typeof process` and `typeof require` are `'undefined'`; environment variables were not visible. Network access from Sort code and file functions in `combineBySql` are **unmeasured**. Source: r5.py, r6.py.
- **M-MCP-44 [measured, review 2]** A `get_node_types` call with one valid and one invalid node (version `'99'`) returns, **without isError**, a text that starts with `# TypeScript Type Definitions`; the section `# Errors` with `Version '99' not found …` appears only from character 6,423. A single call with an invalid version, in contrast, starts directly with `# Errors` [M-MCP-32]. Source: r2.py.
- **M-MCP-45 [measured]** State after review round 2: public `GET /workflows?limit=250` returns 0 workflows, `GET /tags` 0 tags (21.09., checked read-only). With the workflows, executions 115–119 are deleted [F-LIFE1].

## Instance MCP — build of phase 1a (main session, 21.09.)
- **M-MCP-46 [measured, main session]** `newCredential('Slack Bot')` in SDK code, without such a credential existing: the stored node has **no** `credentials` field; `autoAssignedCredentials` is empty. The check `CREDENTIAL_UNKNOWN_ID` does not apply to it, the missing credential is a task for the user. Source: newcred.py.
- **M-MCP-47 [measured, main session]** Answers M7, part 1: a **pinned AI Agent node does not call its subnodes**. Manual → Agent with an `lmChatOpenAi` subnode without a credential; only trigger and agent pinned: `success`, the agent returns the pinned value (execution 121). A call of the model would have failed without a credential. Source: m7_m9.py.
- **M-MCP-48 [measured, main session]** Answers M7, part 2: a **pinned subnode does not replace the model**. Only the `Model` subnode pinned, the agent not: `status: error`, "Error in sub-node Model" (execution 122). The root is therefore always pinned, never a single subnode (design §3.3). Source: m7_m9.py.
- **M-MCP-49 [measured, main session]** Answers M9: `test_workflow` with `timeout: 5` on a Wait of 25 s answers after 6.3 s with `{executionId:'123', status:'error', error:'Workflow execution timed out after 5 seconds'}`. The execution then stands at `canceled` ("The execution was cancelled manually") and stays there. Source: m7_m9.py.
- **M-MCP-50 [measured, main session]** `validate_workflow` on code that n8n cannot parse (unknown node type) answers with `isError: true` **and** the verdict `{"valid": false, "errors": ["Failed to parse … Unrecognized node type: …"]}`. Whoever reads only `isError` reports a finding as a broken tool. Source: smoke.py.
- **M-MCP-51 [measured, main session]** Without `/rest`, n8n does not reveal its version: neither `/healthz`, `/healthz/readiness` nor `/api/v1/…` send a version header, and the MCP's `serverInfo` is `1.1.0` (the MCP server version). A version warning at runtime thus has no source; `tested_n8n_version` stays as a note for upgrades. Source: curl -D.
- **M-MCP-52 [measured, main session]** The plugin against the instance, once through all tools: create, tag and re-check (0 findings), test run execution 120 `success` with a pinned webhook trigger and live-run Set/Respond ("Hello Ada"), `removeTags` of the own tag rejected, a webhook path emptied via `setNodeParameter` immediately reported as `WEBHOOK_PATH_EMPTY`; each call with exactly one status line; afterwards 0 workflows. Source: smoke.py.

## Instance MCP — big review of phase 1a (21.09.)
- **M-MCP-53 [measured, main session + review 3]** Sub-workflows in the test. B = executeWorkflowTrigger → httpRequest (`/healthz`), A = manualTrigger → executeWorkflow (`source: database`, B):
  - Only A's trigger pinned: execution 135 starts B as an execution of its own, 136, and B's HTTP node runs **live** (response `{"status":"ok"}`). B's nodes lie outside A's pin plan; no check on the caller can limit them.
  - Trigger **and** calling node pinned: execution 138 `success`, the calling node returns the pinned value, B afterwards has **no** execution.

  Source: review/subwf_probe.py, fix/subwf_pinned.py.
- **M-MCP-54 [measured, main session + review 3]** The instance MCP of 2.39.9 is **stateless**: `initialize` sends no header `mcp-session-id`, and `tools/call` with a wrong or missing session ID is answered with 200. A client that ties the handshake to the session ID sends it anew before every call: 3 instead of 1 of the 100 requests per 5 minutes. A 429 then hits the `initialize` first (retry-after 26 measured). Source: fix/sess.py, review/p1.py, p2.py, p9.py.
- **M-MCP-55 [measured, main session + review 3]** The storage settings individually, one test run each:
  - only `saveManualExecutions: false` → execution 140, public GET **404**,
  - only `saveDataSuccessExecution: 'none'` → execution 139, stored (mode `manual`),
  - only `saveDataErrorExecution: 'none'`, run with an error → execution 137, stored.

  Only `saveManualExecutions: false` thus leaves a test run unstored. Source: fix/save_manual.py, review/save_probe.py, save_probe_err.py.
- **M-MCP-56 [measured, review 3]** An `ai_tool` edge from a **deactivated** `toolCalculator` into an HTTP Request node is accepted by `update_workflow` (3 operations applied). An `ai_tool` edge from a NoOp is rejected by n8n ("does not produce an ai_tool output"). The old pin plan therefore took the HTTP node for a root with only allowed subnodes and let it run live (execution 131). Source: review/p8.py, p8b.py.
- **M-MCP-57 [measured, review 3]** `get_workflow_details` with `detailLevel: 'execution'` returns metadata, `versionId`, `nodeCount`, `settings`, `tags` and `triggerInfo`, but **no** `nodes` and `connections`. Only `'full'`, n8n's own default, returns them. Source: review 3, probe on zz-probe-rv-ops.
- **M-MCP-58 [measured, review 3]** An IF whose item goes into the false branch appears in runData as `main: [[], [{json: …}]]`: output 0 empty, output 1 with the item (execution 129). Source: review/p6.py.
- **M-MCP-59 [measured, review 3]** `search_nodes(['mcp client'])` returns `@n8n/n8n-nodes-langchain.mcpClient` (v1.1, "Standalone MCP Client"), `mcpClientTool` (v1.4, "Connect tools from an MCP Server") and `mcpRegistryClientTool` ("(internal)"). The first two call any MCP endpoint URL; M-MCP-42 knew only the third. Source: review 3.
- **M-MCP-60 [measured, main session]** `mcpTrigger` v2: `authentication?: 'none' | 'n8nOAuth2' | 'bearerAuth' | 'headerAuth'`, default `none` with the builderHint "Only select an authentication method when the user explicitly asks"; credentials `httpBearerAuth` or `httpHeaderAuth`. Without auth, a published MCP trigger is open to everyone who reaches n8n. Source: fix/mcptrigger.py.
- **M-MCP-61 [measured, main session]** `get_workflow_execution` on a **failed** execution (code throws, execution 147 `error`) answers without `isError` and without `error` at the top level: `{execution: {…, status: 'error'}}`, with `includeData` additionally `data.resultData.error`. Reading a failure is thus not a tool error. Source: fix/failed_exec.py.
- **M-MCP-62 [measured, local: Python against Node]** `http://evil.test\@allowed.example.org/x`: Python's `urlparse(...).hostname` is `allowed.example.org`, Node's `new URL()` and `url.parse()` read `evil.test`. The httpRequest definition additionally has `options.proxy` and `options.pagination.pagination.nextURL` (v3–v4.5). That n8n passes the URL on to axios like this is inferred, not measured live; the live check therefore rejects such URLs. Source: fix review, fixreview/u.py, u.js, work/nodes.json. Re-checked in the second fix review with a fuzz over 306,880 URL forms against `new URL` and `url.parse`: with backslash and control characters rejected everywhere, non-ASCII, space, `@` and `%` rejected in the host only, the check never yields an allowed host where Node reaches a different one. Source: fixreview2/fuzz2.py, fuzz2.js.
- **M-MCP-63 [from the node dump]** Among the 990 node types, only `n8n-nodes-base.emailReadImap` carries the group `trigger` without ending in `Trigger` (besides webhook, cron, interval). Without `triggerNodeName`, n8n starts the first enabled trigger in node order (`findEnabledEligibleTrigger`). Source: work/nodes.json, n8n source `mcp.utils.js`, fix review 2.
- **M-MCP-64 [from the node dump]** `@n8n/n8n-nodes-langchain.lmChatOpenAi` (v1 to 1.3) has, from v1.3, the switch `responsesApiEnabled` with default `true` (Responses API) and under `options` a `baseURL` of its own. An n8n workflow can thus address a Responses-compatible endpoint as a chat model. Whether n8n fully serves a foreign endpoint with this is not measured. Source: work/nodes.json, E8.

## Phase 1b: publishing, triggering, observing
- **M-MCP-65 [measured]** Public `GET /executions/{id}` carries `workflowVersionId`: the `versionId` of the draft on which the execution ran (test run 163 = the workflow's `versionId`). In the **list** `GET /executions` the field is `null`. `addTags` does not change the `versionId`. Thus publish checks "this state is tested" exactly. Source: fix/p1b.py, fix/p1b2.py.
- **M-MCP-66 [measured, answers M11]** Responses of the MCP without `isError`:
  - `publish_workflow` with `versionId` → `{success:true, workflowId, activeVersionId}`; afterwards `versionId` = `activeVersionId`, `active: true`.
  - A second workflow on the same webhook path: `{success:false, activeVersionId:null, error:'There is a conflict with one of the webhooks.'}`.
  - On an archived workflow: `{success:false, error:"Workflow '<id>' is archived and cannot be accessed."}`.
  - `unpublish_workflow` twice: both `{success:true, workflowId}`.
  - `archive_workflow` → `{archived:true, workflowId, name}`; a second time `isError` with the same message as above. Afterwards `isArchived: true`, `active: false`.

  Source: fix/p1b.py.
- **M-MCP-67 [measured]** Production webhook and executions:
  - `responseMode` `onReceived` answers immediately `{"message":"Workflow was started"}` without an execution ID, `lastNode` only after the run with the data of the last node (3 s with a Wait of 3 s).
  - An unknown path: 404 with `{"code":404,"message":"The requested webhook \"POST <path>\" is not registered.", "hint": …}`.
  - `GET /executions?workflowId=` without `status` lists only **finished** executions. A running one appears only under `status=running` (mode `webhook`). `startedAfter` likewise filters only finished ones.
  - A Wait node without `unit` waits hours, not seconds (the first probe got stuck and vanished with the deletion of the workflow).

  Source: fix/p1b.py, fix/p1b2.py (executions 163–168).
- **M-MCP-68 [measured]** Public `GET /workflows/{id}` of a published workflow returns `activeVersionId` and `activeVersion` with `nodes` (including `webhookId`), `connections`, `versionId`, `workflowPublishHistory`. Source: fix/p1b.py.
- **M-MCP-69 [measured, answers M10]** Our `mcp_client` against an n8n MCP trigger that is not there: `connect_all` does not throw, reports one error per server (closed port: `ConnectTimeout` after 5 s; n8n reachable, path unknown: `McpError: Session terminated`) and continues starting. The tool list is empty, a call gives `MCPConnectionError … is not connected`. There is no automatic reconnect. Source: fix/m10.py (`ExternalServerPool`, timeout 5 s).
- **M-MCP-70 [measured, 2026-09-22]** Public API: a missing execution and a missing workflow answer `404` with `application/json` and `{"message":"Not Found"}`. Test run 189 (MCP `test_workflow`) appears with `mode: manual`, production run 190 (webhook) with `mode: webhook`, both with the same `workflowVersionId`, because draft and published state were equal. List entries carry `mode` and `startedAt`. `limit=250` is accepted. Source: probe against the test instance after the E2E run (e2e_get).

## Embedding, CORS and observation
- **F-EMB1 [measured/documented]** Embedding and transport:
  - The editor sends `X-Frame-Options: SAMEORIGIN`, hard-wired.
  - The cookie `n8n-auth` is `HttpOnly; SameSite=Lax; Max-Age=604800`; on the test instance without `Secure` [F-DEP1].
  - The generic compose file publishes port 5678 directly, without TLS [documented: `docker-compose.yml:8-9`].

  Source: curl; `packages/cli/src/server.ts`.
- **F-EMB2 [measured]** `/api/v1`, `/rest` and `/mcp-server/http` send no CORS headers. Source: curl OPTIONS.
- **F-OBS1 [measured]** Log streaming is license-locked. The editor push delivered 0 events for production runs in 25 s. Source: connect.
- **F-OBS2 [measured/documented]** The OTel settings are readable and set to off. The external hooks need a change to the container. Source: `/api/v1/settings/otel`; docs.

## Our side
- **F-OUR1 [measured]** In `config/` and `src/agent_system/` there is no n8n reference. A plugin folder containing only docs is silently skipped by the discovery (DEBUG). Source: probe_discovery.py.
- **F-OUR2 [documented]** The handler contract requires `{"status":"success"|"error"}`; the error net does not recognize `failed`. Status lines: exactly one `end` or `error`, at most 140 characters. Source: `tools/base.py:86-90`; `tests/plugins/test_status_end_lines.py`.
- **F-OUR3 [documented]** The framework validates no arguments and truncates no results (except context_engineer, pre-layer T). Source: `references/tools.md:71-93`.
- **F-OUR4 [documented]** `${VAR}` applies only to names from `[A-Z0-9_]`. An unset variable gives `""` plus a WARNING. Source: `config/environment.py` (`expand_env`).
- **F-OUR5 [documented]** Agent and tool YAMLs under `src/plugins*/*/agents/*.yaml` are included by the glob in `config/config.yaml:18`. There they stand under `plugins:` → `servers:`. Source: `src/plugins/research/agents/*.yaml`.
- **F-OUR6 [documented]** httpx and aiohttp are core dependencies. Source: `requirements/core.txt:9,11`.
- **F-OUR7 [documented]** `enabled` defaults to false. Plugin config is not validated. Source: `config/plugins.yaml:16-17`.
- **F-OUR8 [documented]** Pattern "no tools without a key". Source: `tavily_search/schema.yaml:4`, `server.py:57-60`.
- **F-OUR9 SUPERSEDED → F-DEP5** Formerly: an `.env` in `docs/deploy/` would not be git-ignored.
- **F-OUR10 [documented]** Allowlists: `tools.allowed: ["+…"]` extends the list, without `+` it replaces. An agent with visibility `tool`/`both` appears for callers with `<agent>/*` as `<agent>_execute_task`. Source: `config/agents/agents.yaml:6-32`; `runtime.py:154-159`; `tool_discovery.py:156-222`.
- **F-OUR11 [measured]** No active agent reaches the root SAM `sub_agent_manager`. Source: probe_sam.py.
- **F-OUR12 [documented]** `wake_blocked()` returns "" or a reason. `wake_session()` starts a **new** process and takes effect only while the process that holds the work is alive. Source: `core/session_presence/wake.py`; pattern `terminal/server.py:306-340,441-462`.
- **F-OUR13 [documented]** The only teardown is `stop_plugin` on the object from `PLUGIN_FACTORY`. Source: `plugins/capabilities.py:182-190`; `test_pluginsystem_teardown_hook.py:59`.
- **F-OUR14 [documented]** The API binds `127.0.0.1:8000`. Source: `config/config.yaml:56-57`.
- **F-OUR15 [documented]** `/run` always answers with `text/event-stream`. Source: `app.py`, search term `media_type="text/event-stream"`.
- **F-OUR16 [documented]** Plugin routes accept only JWT or cookie, no `X-API-Key`. Source: `plugins/web_adapter.py:335,342`.
- **F-OUR17 [documented]** API keys exist only per user; there is no service account. Source: `api/auth_endpoints.py:393-424`.
- **F-OUR18 [measured]** `src/agent_system` provides no MCP server. Source: grep.
- **F-OUR19 [measured]** `validate_plugin.py src/plugins/n8n` ends with exit 1 without `plugin.toml` and prints no reason. Cause: `validate()` returns at `:94-95` before `_report_results()`.
- **F-OUR20 [measured]** Allowlists with single tools are common in the repo. Source: grep in `config/agents`.
- **F-OUR21 [measured, review]** No existing agent reaches another via `<agent>/*`. Source: grep.

## External
- **F-EXT1 [documented]** `czlonkowski/n8n-mcp` (MIT) builds its catalog from npm packages, not from the instance, and has telemetry on by default. Source: GitHub README, PRIVACY.md.

## Measurement points

**Done:**
- M1/M2 (`/rest` pinData): see F-RUN1. For `test_workflow`, [M-MCP-3] and [M-MCP-39] answer both questions: the caller's pinData takes effect on Set, Code and HTTP Request.
- M3 (Python validator against the server): dropped, because no Python rebuild exists any more.
- M6: dropped, because writing is no longer done via PUT.
- "Endpoint for rotating the MCP key": [M-MCP-H2].
- "Behavior of the instance MCP tools": [M-MCP-H1]–[M-MCP-44].
- "Is the code sandbox networkless?": no [M-MCP-38].
- M12 "Does the read key see the tags?": yes [F-AUTH8].
- M7 "Does pinData take effect on AI subnodes?": the pinned root node does not call them [M-MCP-47]; a pinned subnode replaces nothing [M-MCP-48].
- M9 "What does `test_workflow` return after the timeout?": `status: error` with a timeout message, execution `canceled` [M-MCP-49].
- M8 "When does an MCP session expire?": dropped, 2.39.9 issues no session [M-MCP-54].
- M10 "mcp_client with an unreachable server": startup continues, no automatic reconnect [M-MCP-69].
- M11 "Rejections of `publish_workflow`": `success:false` with text, without `isError` [M-MCP-66].

**Open:**
- **M4:** How does the MCP key of a member user behave [M-MCP-29]? That needs a second user on the instance and thus the operator's OK.
- **M5:** How does `test_workflow` behave with a schedule or polling trigger (pinned)?
- **M13:** Does `prepare_workflow_pin_data` deliver schemas after a first execution [M-MCP-2]?

**In addition unmeasured:**
- the maximum of the HTTP timeout,
- whether `limit>250` is capped,
- how a webhook answers a wrong HTTP method,
- how `explore_node_resources` behaves without a credential,
- whether the sandbox of `langchain.code` matches that of the Code node,
- whether Sort code has network access and what file functions `combineBySql` permits [M-MCP-43].
