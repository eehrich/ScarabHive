# Tool Script Plugin — Design

**Status:** Concept v2 — panel-reviewed (24 confirmed findings incorporated; v1's
"zero core change" and "sandbox is just Python" claims were wrong and are
corrected below)
**Plugin type:** `tool_script` (working title; instance name free, e.g. `pipe`)
**Related:** `script_interpreter` (sandbox engine reused; plugin surface later
deprecated, see §14), `json_store` (reference passing), `sub_agent_manager`
(single-tool pattern, error-message lessons)

---

## 1. Problem

Agents that chain tools pay a **token tax** on every hop: the result of tool X
enters the context, and the agent re-types (or paraphrases) it as arguments to
tool Y. This is:

- **Expensive & slow** — output tokens are the costly, latency-bound part of a
  turn; a 5-call chain costs 5 LLM round-trips plus the re-typed payloads.
- **Lossy** — LLMs corrupt payloads they re-type: `\n` un-escaped, values
  shortened, commas dropped. This exact failure produced the v6 synopsis JSON
  corruption and motivated `json_store`.

`json_store` fixed this for *one* data shape (JSON documents, passed by
reference, merged in code). The generalization is: **let the agent write a
small script that orchestrates tool calls server-side.** Intermediate results
never enter the context; only the script (small) and the final result (small)
cost tokens.

This is the industry-validated "code execution / CodeAct" pattern (Anthropic's
code-execution-with-MCP writeup reports >90 % token savings on chains).

**Honest economics (review finding):** a *failed* script costs one full retry
turn plus an error payload. The break-even depends on first-try validity, which
depends on how close the sandbox is to real Python (§5) and how good dispatch
errors are (§6). The pilot therefore ships with a measurement gate (§12) —
tokens and turns per v6 ritual step, script-mode vs. direct-mode, before wider
rollout.

### Why not a declarative pipe tool?

A "call X, feed output into Y.argZ" pipe covers only the trivial case. Real
chains need transformations between hops. A pipe schema grows into a homemade
DSL no LLM knows from training. Python *is* that DSL, pre-trained — provided
the sandbox subset is honest (§5). The pipe idea is a strict subset of the
script idea with worse ergonomics — rejected.

---

## 2. Goals / Non-goals

**Goals**

1. One tool: agent submits a Python script; the plugin executes it in the
   sandbox; the script calls the agent's tools via `call_tool()`.
2. Intermediate tool results are plain Python values inside the script — never
   serialized into the LLM context.
3. Same security envelope as normal tool calls: the agent's `tools.allowed`
   **and `tools.blocked`** patterns govern what a script may call.
4. Actionable errors with **partial-state reporting** and **schema-validated
   parameters** so a failed chain is diagnosed in one turn.

**Non-goals**

- General Python (imports, filesystem, network, threads) — the *only*
  side-effect channel is `call_tool()`.
- Replacing sub-agents. LLM-reasoning steps stay in `sub_agent_manager`.
- Parallel execution (v1 sequential; §12).
- Durable workflows. A script is one tool call: seconds-to-a-minute.
- **External MCP-server tools (v1).** They take a different execution branch
  than plugin tools and are explicitly out of scope until Phase 3 (§6.2).

---

## 3. Plugin + one core helper — decision (revised)

v1 of this doc claimed a plugin needs zero core changes. The review disproved
that on three counts:

1. **Tool-name→server resolution:** `_get_server_from_any_registry()` takes a
   *server* name; scripts pass flat *tool* names (`v6_json_manage_json`). The
   flat-name→server mapping is built during schema expansion and lives on the
   per-request conversation context — a plugin cannot faithfully rebuild all of
   `tool_execution`'s branches (own tools, config agents, external MCP,
   registry fallbacks).
2. **Allowlist semantics:** the *effective* filter is
   `ToolSchemaBuilder._is_tool_allowed(tool, server, patterns)` matching the
   full path `server/tool` — plus **blocked patterns** applied after allowed.
   The agent-level 2-arg `_is_tool_allowed` (server.py) matches server names
   and returns `False` for every flat tool name against the standard
   `server/*` config form. Re-implementing this in a plugin would both
   over-block (unusable) and under-block (`tools.blocked` bypass).
3. **`call_with_status`:** dispatch must go through `call_with_status` when
   the server provides it — many tools require the injected `_status` and
   crash without it.

**Therefore the core helper is REQUIRED, not optional:**

```python
# on the agent server (extracted from tool_execution._execute_plugin_tool)
async def dispatch_tool_call(self, tool_name: str, params: dict,
                             *, context: dict) -> Any:
    """Resolve tool_name -> (server, server_name); enforce this agent's
    tools.allowed AND tools.blocked against the full path server/tool via the
    ToolSchemaBuilder matcher; inject runtime params (_session_id, _user_id,
    _request_id, _agent_name, _agent, _cancellation_token, _status);
    call via call_with_status/call. Raise ToolDispatchError with an
    agent-actionable message on unknown tool / not allowed."""
```

`tool_execution._execute_plugin_tool` is refactored to use the same helper
(single source of truth — no drift), which is the main cost and the main
benefit of the core change. The plugin itself stays thin: sandbox embedding +
`call_tool` bridging + result shaping.

Resolution inside the helper: longest-prefix match of registered server names
against the flat tool name (`v6_json_manage_json` → server `v6_json`), then
the existing registry lookups; own-tool prefix and config-agent branches as in
`tool_execution` today; **external MCP names → explicit "not supported in
scripts (v1)" error** rather than silent failure.

---

## 4. Tool surface

Single tool (tool-slot economy, same pattern as `json_store` / SAM):

```
{{ name }}_run_script
  script      (string, required)  Python script; sandbox rules below
  timeout     (number, optional)  overall seconds, capped by config
  dry_run     (boolean, optional) sandbox-syntax + static checks only
```

### How the agent knows tool names and parameters

**The agent's normal tool list is unchanged** — schemas (names, parameters,
descriptions) are in its context as today. `run_script` is one *additional*
tool. Scripts call tools **by exactly the name and parameters from the
agent's own tool list**; the tool description states this verbatim. Direct
call vs. script is the agent's per-situation choice: single operation →
direct; chain of ≥2 dependent calls → script. No separate discovery mechanism
is needed (allowlist-scoped agents have small tool lists; the schemas are
already paid for).

**Parameter validation (review finding):** before dispatch, `call_tool`
validates the provided params against the target tool's JSON schema (which the
plugin has server-side). A typo'd or missing param fails **immediately at the
call site with the tool's schema echoed in the error** — instead of a silent
mis-execution or a failure three hops later.

### Result (success)

```json
{
  "status": "ok",
  "result": <value of the script's `result` variable>,
  "calls": [
    {"tool": "v6_json_manage_json", "ok": true, "ms": 12},
    {"tool": "debate_forum_post_message", "ok": true, "ms": 48}
  ],
  "output": "<print() output, capped>"
}
```

### Result (failure) — the resumability contract

```json
{
  "status": "error",
  "error": "ToolCallError in line 7: v6_json_manage_json -> Document 'x' not found. Existing: ['synopsis']",
  "line": 7,
  "calls": [
    {"tool": "v6_json_manage_json", "ok": true, "ms": 15},
    {"tool": "v6_json_manage_json", "ok": false, "ms": 4, "error": "..."}
  ],
  "committed_side_effects": "calls 1-1 committed; call 2 failed; nothing after line 7 executed",
  "variables": {"delta_id": "doc_a1b2c3d4", "big_payload": "<dict, 14.2k chars — omitted>"}
}
```

- Executed side effects are listed, **never rolled back silently**.
- **Variables snapshot is capped (review finding):** scalars and short strings
  verbatim; anything larger reported as type + size only. The snapshot must
  not dump the very payloads the design keeps out of context.
- **Resume honesty (review finding):** mid-tier models tend to re-send the
  whole script, re-executing committed side effects. Mitigations: (a) the tool
  description instructs "on failure, send a NEW script continuing after the
  last committed call — do not repeat calls marked ok:true"; (b) the pilot
  scopes to chains whose steps are idempotent or collision-guarded
  (json_store `if_exists`, merge_doc); (c) non-idempotent steps (forum posts)
  go LAST in a chain wherever possible.

### Error contract for `call_tool` (revised)

Tools in this system **do not raise on domain failure** — they return
`{"status": "error", "error": "..."}` (json_store, SAM, ...). Therefore:

- `call_tool` raises `ToolCallError(message)` when the result is a dict with
  `status == "error"` (message = the tool's `error` verbatim), on dispatch
  errors (unknown tool, not allowed, schema-invalid params), and on transport
  errors/timeouts.
- Any other result is returned as data. Tools that don't follow the status
  convention return whatever they return — the script inspects it.
- `ToolCallError` is a seeded name in the sandbox, so `try/except
  ToolCallError:` works (§9.3).

---

## 5. Script environment (revised — the sandbox is a *subset*, treat it as such)

### Reused from `script_interpreter` (SafeExecutor)

`SafeExecutor` (AST interpreter, ~1.5 k LOC in
`plugins/script_interpreter/safe_executor.py`) provides: whitelisted builtins
(kwargs supported), no imports, no dunder access, banned `_`-prefixed names,
loop guards, output caps, `try`/`except`/`raise`, user functions, lambdas,
LLM-oriented error formatting (`format_error_for_llm`), sync execution in a
worker thread. It is embedded **as a library**; the `script_interpreter`
plugin surface is untouched (until §14).

`tool_script` creates a **fresh executor per call** (chains are
self-contained; no cross-call state, no reset ceremony, no shared-lock
concerns between scripts).

### What the review found missing — prerequisites before the pilot

The "LLMs write Python" argument only holds if the subset doesn't ambush the
model. Confirmed gaps that break *idiomatic* code today:

| Gap | Impact | Plan |
|---|---|---|
| **No slicing** (`x[1:3]`, `s[:100]`) | hard fail, unhelpful error | **extend SafeExecutor (Phase 1 prerequisite)** |
| `str.format` unsupported | fail | extend or document (`+`-concat works) |
| `dict(a=1)` kwargs silently dropped | silent wrong data | fix (kwargs exist in the call path) |
| `_`-prefixed variable names banned | surprising fail | document in cheatsheet |
| `json` module absent | n/a — `call_tool` results arrive parsed | document |
| No custom-builtin registry (`safe_builtin_function` is a hardcoded if/elif chain) | `call_tool` can't be added as "builtin" | **seed `call_tool`, `ToolCallError`, `log` as pre-set variables** in `executor.variables` — verified workable, kwargs included |

Deliverable alongside Phase 1: a **sandbox cheatsheet** embedded in the tool
description (supported constructs, the gaps above, 2 canonical examples). The
model must never have to guess the dialect.

### Added by `tool_script`

```python
call_tool(name, **params) -> dict | list | str   # §4 error contract
log(msg)                                          # -> _status progress + trace
```

- `params` must be plain data (str/int/float/bool/None/list/dict) — anything
  else is rejected before dispatch.
- **Results are sanitized to plain data (review finding):** every `call_tool`
  result passes a JSON round-trip (`dumps(default=str)` → `loads`) before it
  enters the sandbox. Tool results carrying live objects (server references,
  tokens) can therefore never leak into script variables — closing the
  "results are the unvalidated back-channel" hole. The round-trip also yields
  the size measurement for the per-result cap (§6.5).

---

## 6. Dispatch path & security (revised)

All dispatch goes through the core helper (§3). Per `call_tool`:

1. **Resolution:** flat tool name → (server, server_name) via longest-prefix
   registry match + the existing fallback branches. Unknown → `ToolCallError`
   listing close matches from the agent's own tool list.
2. **Authorization — full fidelity to schema-build:** match
   `f"{server_name}/{tool_name}"` against the agent's `tools.allowed` **and
   then `tools.blocked`**, using the same matcher schema-build uses
   (`ToolSchemaBuilder._is_tool_allowed`, extracted to be callable from the
   helper). A tool the LLM cannot see is a tool the script cannot call —
   in both directions. Plugin-config `allowed_tools`/`blocked_tools` intersect
   (never widen). Own tool + all `tool_script` instances always blocked
   (no recursion).
3. **Param schema validation** (§4) — before injection, so validation can
   never be confused by runtime params.
4. **Runtime param injection** — `_session_id`, `_user_id`, `_request_id`,
   `_agent_name`, `_agent`, `_cancellation_token`, `_status`; injected *after*
   validation of script-supplied data, so scripts cannot forge them
   (top-level keys are overwritten unconditionally).
   **Request-id lineage (review finding):** each inner call gets a child id
   `f"{parent_rid}_ts{n:02d}"` (mirroring the `_sub_` convention) so
   message_debugger/cost attribution can distinguish hops.
5. **Caps:** `max_tool_calls`/script (default 20); `per_call_timeout`
   (default 60 s); **`max_call_result_bytes` (default 512 k, review finding)**
   enforced on the JSON round-trip — an oversized result is rejected with
   "store it via json_store and pass the reference"; `max_result_chars` on the
   script's `result` (default 20 k, same advice).
6. **Timeouts — honest semantics (review finding):** the overall `timeout` is
   enforced by SafeExecutor for pure-Python segments and re-checked **between**
   inner calls; it cannot interrupt an in-flight inner call. The true
   wall-clock bound is `min(timeout enforced between hops) + one
   per_call_timeout overhang`, worst case `max_tool_calls × per_call_timeout`.
   Config caps must be set with that formula in mind.
7. **Cancellation — honest semantics (review finding):** the forwarded
   `_cancellation_token` is checked between hops; an in-flight inner call
   completes (or hits `per_call_timeout`) before the script aborts. Inner
   tools that check the token themselves (long-running ones do) abort sooner.
8. **Concurrency:** one running script per session (per-session semaphore).
   Scripts block a worker thread for their full duration; the semaphore plus
   `max_tool_calls × per_call_timeout` bounds worst-case pool pressure.
9. **Namespace reality (review finding, accepted):** data params like
   json_store's `namespace` remain caller-controlled — a script can pass any
   namespace *exactly like the agent could directly*. No new capability, no
   new mitigation; noted for completeness.

### Async bridge

Tool servers are `async`; SafeExecutor is synchronous in a worker thread. The
plugin's async entrypoint captures the running loop, then:

```python
future = asyncio.run_coroutine_threadsafe(
    agent.dispatch_tool_call(name, params, context=ctx), main_loop)
result = future.result(timeout=per_call_timeout)
```

The event loop is never blocked; the worker thread waits.

---

## 7. Observability (revised — honest v1)

What a plugin **can** do without core changes:

- Log each inner call exactly like `tool_execution` does (`Invoking tool ...`
  / `Tool ... returned: ...`) with a `via=tool_script` marker — `cli.log`/
  `api.log` forensics keep working.
- Update `_status` per hop (`script 2/5 — merging delta`), fed by `log()` and
  automatic per-call updates.
- Return the full `calls` trace in the tool result (the agent-facing audit).
- Child request-ids per hop (§6.4) so DB-side records that inner tools
  themselves create (e.g. message_debugger entries from nested LLM calls) are
  attributable.

What v1 explicitly does **not** deliver (review finding — requires a core
event channel that plugins lack): nested `mcp_call` events in the live event
stream / message_debugger timeline entries for the inner hops themselves. The
main panel shows one `run_script` call with its final result. **Phase 2** adds
an event-emitter handle to the dispatch helper if the pilot shows the trace +
logs are not enough.

---

## 8. Interplay with `json_store` (by-reference piping)

No special bridge needed — `json_store` is just another tool. Together they
complete the picture: big payloads flow by reference through scripts; a
too-big script `result` or `call_tool` result gets a store-doc + reference
instead (enforced by the §6.5 caps, advised by their error messages).

---

## 9. Worked examples (validated against the sandbox subset — no slicing, no
f-strings, `+`-concat only)

### 9.1 v6 coordinator ritual step (today: 4 agent turns → 1 script call)

```python
merge = call_tool("v6_json_manage_json", operation="merge_doc",
                  source=delta_id, doc="synopsis", namespace=group_id)
delta = call_tool("v6_json_manage_json", operation="read",
                  doc=delta_id, namespace=group_id)
call_tool("debate_forum_post_message", channel_id=channel_id,
          content="### 2.2 World\n```json\n" + delta["json"] + "\n```")
result = {"merged_keys": merge["merged_keys"]}
```

(Non-idempotent forum post deliberately last — §4 resume guidance.)

### 9.2 Fan-out over a list

```python
docs = call_tool("v6_json_manage_json", operation="list", namespace=group_id)
summaries = []
for d in docs["docs"]:
    o = call_tool("v6_json_manage_json", operation="outline",
                  doc=d["doc"], namespace=group_id, depth=1)
    summaries.append({"doc": d["doc"], "outline": o["outline"]})
result = summaries
```

### 9.3 Tolerant chain with fallback

```python
try:
    r = call_tool("v6_json_manage_json", operation="read", doc="synopsis",
                  namespace=group_id)
except ToolCallError:
    call_tool("v6_json_manage_json", operation="write", doc="synopsis",
              namespace=group_id, data={})
    r = call_tool("v6_json_manage_json", operation="read", doc="synopsis",
                  namespace=group_id)
result = {"chars": r["chars"]}
```

(Works because `call_tool` converts `status=="error"` results into raised
`ToolCallError`s — §4 — and `ToolCallError` is a seeded sandbox name.)

---

## 10. Configuration (schema.yaml sketch)

```yaml
pipe:
  type: tool_script
  enabled: true
  config:
    timeout: 120                  # overall; see §6.6 for real semantics
    per_call_timeout: 60          # seconds per inner tool call
    max_tool_calls: 20            # per script
    max_call_result_bytes: 524288 # per inner result (JSON size); larger -> store+ref
    max_result_chars: 20000       # script `result`; larger -> store+ref
    max_output_length: 4000       # print()/log() buffer
    allowed_tools: []             # optional narrowing (never widening)
    blocked_tools: []             # extra blocks; tool_script instances always blocked
```

The agent gets the tool via the normal allowlist, e.g. `- "pipe/*"`.

---

## 11. Failure modes & mitigations (revised)

| Failure | Mitigation |
|---|---|
| Script calls a tool outside the agent's effective toolset | Full-fidelity re-check: allowed **and blocked**, full-path matcher, in the core helper (§6.2) |
| Doc-v1 re-check bug (wrong matcher → everything rejected) | Fixed by design: the helper reuses the schema-build matcher; a unit test asserts parity between schema-build filtering and dispatch filtering for the same config |
| External MCP tool name in a script | Explicit "not supported in scripts (v1)" error (§3), not silent failure |
| Inner tool needs `_status` | Dispatch via `call_with_status` in the helper (§3) |
| Typo'd tool name / param name | Close-match suggestions; JSON-schema param validation at the call site (§4) |
| Tool signals failure by return value | `status=="error"` → raised `ToolCallError` (§4) |
| Recursion | `tool_script` instances always blocked |
| Endless loop / runaway chain | SafeExecutor guards + `max_tool_calls` + between-hop deadline |
| Mid-chain failure | try/except in-script; else partial report with trace, line, capped variables (§4) |
| Model re-runs whole script after failure | Tool-description resume instruction; idempotent-first pilot scope; non-idempotent hops last (§4) |
| Live objects in tool results | JSON round-trip sanitization of every result (§5) |
| Huge inner results / memory | `max_call_result_bytes` per result + store-and-reference advice (§6.5) |
| Error report dumps big payloads | Variables snapshot capped: scalars verbatim, else type+size (§4) |
| Script forges `_session_id` / `_agent` | Data-only param validation, unconditional runtime overwrite after validation (§6.4) |
| Sandbox subset ambushes the model | SafeExecutor extension (slicing at minimum) + cheatsheet in tool description (§5) |
| Thread-pool pressure / concurrency | One script per session; bounded worst-case wall-clock (§6.8) |
| Agent uses scripts for single calls | Tool description: direct call for 1 op; script for ≥2 dependent calls |

---

## 12. Phasing (revised)

- **Phase 0 (prerequisites):**
  - Core helper `agent.dispatch_tool_call(...)` extracted from
    `tool_execution._execute_plugin_tool`; `tool_execution` refactored onto it;
    **parity unit test** schema-build filter ↔ dispatch filter.
  - SafeExecutor: add slicing (+ close the `dict(a=1)` silent-drop), write the
    sandbox cheatsheet.
- **Phase 1 (MVP):** plugin skeleton; per-call SafeExecutor embedding;
  `call_tool`/`ToolCallError`/`log` seeding; param schema validation; result
  sanitization + caps; call trace + partial-state errors; per-session
  semaphore; unit tests (mocked agent) + security tests (allowlist/blocked
  bypass, recursion, object smuggling via result, oversized result, timeout,
  forged runtime params).
- **Phase 2:** pilot in the v6 coordinator (ritual step as script) with the
  **measurement gate**: tokens + turns + failure rate script-mode vs.
  direct-mode over N runs; nested event channel in the dispatch helper if
  trace+logs prove insufficient; `dry_run`.
- **Phase 3 (on demand):** external MCP dispatch branch; `parallel([...])`;
  sub-agent calls from scripts (own timeout regime); §14 deprecation.

---

## 13. Open questions

1. SafeExecutor extension scope: slicing is mandatory; also f-strings/
   `str.format`? (Cheap wins for model ergonomics; decide during Phase 0 by
   checking what deepseek-chat actually emits in trial scripts.)
2. Should the failure report include a ready-made `continuation_hint`
   (the remaining script lines after the failure point) to steer models away
   from full re-runs?
3. `log()` granularity vs. `_status` update frequency (every hop vs. throttled).
4. Config agents (LLM-backed agents as tools): resolvable via the helper, but
   their latency doesn't fit `per_call_timeout=60`. Block in v1 like external
   MCP, or allow with a per-tool timeout override?

---

## 14. Future: `script_interpreter` deprecation

Once `tool_script` is live, a script with zero `call_tool` calls *is* the
calculator use case — `tool_script` is a strict superset, and an instance with
`allowed_tools: []`+`blocked_tools: ["*"]` is security-equivalent to today's
`script_interpreter`. Plan (explicitly NOT part of Phase 1 — no coupling of a
working plugin's removal to the MVP):

1. **Enumerate consumers first** (the June allowlist lesson): find all agent
   YAMLs with `script_interpreter/*` in `tools.allowed`, migrate their config
   and any prompt references to the new tool name.
2. Deprecate the `script_interpreter` plugin surface (server.py wrapper);
   keep `safe_executor.py` + friends as the shared sandbox library that
   `tool_script` imports (move only if imports get awkward).
3. Two functional deltas, both acceptable: variable persistence across calls
   within a session disappears (tool_script is stateless per call — a config
   option if anyone actually needs it), and `validate`/`reset` are subsumed by
   `dry_run`/statelessness.
