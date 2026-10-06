# Loading tools on demand (`tools.deferred`)

With **every** LLM call, an agent sends the schemas of all tools its allowlist
permits. For the coder that was 51 tools with about 49 KB, on every step. A run
never needs most of them: forge only for tickets, OKF writing only at the end of
a piece of work.

With `tools.deferred`, the agent sends rare tools only as a name and a one-line
description. The full schema is added only when the model asks for it. Claude
Code does the same (deferred tools plus `ToolSearch`).

## Configuration

```yaml
tools:
  allowed:
    - "coder_fs/*"
    - "forge/*"
  deferred:
    - "forge/*"      # same patterns as allowed/blocked
```

- `deferred` changes nothing about **what** the agent may do. The allowlist
  remains the only authority. A deferred tool stays permitted, only its schema
  is held back.
- The patterns are matched by the same matcher as allowed/blocked
  (`tool_matches_patterns`).
- Empty or unset: everything stays as before, and `tool_search` does not exist
  then.
- The merge syntax (`+`/`!`) applies here too.

## Flow

1. **Run start:** The deferred schemas are taken out of the tool list. The core
   tool `tool_search` takes their place. Its description lists them with name
   and first sentence.
2. **Loading:**
   - `tool_search(query="select:a,b")` loads exactly these tools.
   - Any other `query` loads the best keyword matches, at most five. A match in
     the name weighs more than one in the description.
   - The schemas are appended **at the end** of the tool list and apply for the
     rest of the run.
3. **Call without loading:** The tool does **not** run, because its arguments
   would be guesses. Instead its schema is loaded, and the model gets an error
   (`ToolNotLoaded`) asking it to repeat the call.
   - Every call to a tool that was loaded **in this step** is rejected. That
     applies to a second call as well as to a call directly after the
     `tool_search` that loaded the tool. The model only gets to see the schema
     in the next step.
   - Such a call never ran. Therefore, like a call blocked by a hook, it does
     not count toward the error streak of auto-escalation.
4. **Next run of the same session:** What the history had already loaded is
   immediately back (`restore`), in the order in which the run loaded it. This
   means tools named by a `tool_search` answer or that were already called. That
   way the run starts with the tool list (and the cache prefix) with which the
   previous one ended.

Code: `src/agent_system/servers/agent/deferred_tools.py`. Wired into
`Agent._initialize_request_and_conversation` (splitting and `restore`), before
every LLM call in the step loop (`restore`) and in
`ToolExecutionManager.execute_tools_streaming` (parameter `intercept`). Pre-
and post-tool hooks do not see `tool_search`, nor a rejected call.

`/context` counts what a run of the session actually sends. For this the API
passes the stored messages through, so the number is also right for a session
that ran in another process. `/tools` and `list_available_tools` show all tools
the agent may call, deferred ones included.

A `deferred` pattern that matches no permitted tool has no effect. The core
writes a warning to the log once per process, and the agent_editor shows it as
the problem "Deferred matches no allowed tool". For an external MCP server entry
(`mcp_servers`) the field is ignored.

**Before every LLM call**, `restore` runs again over the history. It loads
nothing if nothing is new. But it catches tool calls that others wrote into the
history, such as `tool_preload` in the pre-LLM hooks or appended messages. That
way the model never reads a call to a tool whose schema it does not have. The
`restore` at run start is still needed: the pre-LLM hooks of the first step
measure the size through `get_live_tools_schema`.

**Limit, deliberately left as is:** If a pre-LLM hook writes a tool call into
the history, the schema for it does go out, but the hooks of the same chain have
already measured the size without it (context_engineer, context_summarizer). The
deviation is one tool schema for one call. To close it, the shared hook chain
(`hooks/registry.py`) would have to call back after every hook, for every
plugin.

A call with broken JSON gets the parse error. If the tool is not yet loaded, it
loads it anyway immediately, in the order of the calls. That is the same order
that a later `restore` rebuilds from the history.

**Scripts (`tool_script`):** A script may call a deferred tool that the model
never loaded. The permission still comes solely from the allowlist. For the
parameter check and for the "external MCP tool" hint, `tool_script` reads
`Agent.get_run_tool_schemas(session_id)`: the live list plus all deferred
schemas of this session's running run.

## Prompt cache

The tool list sits at the front of the cached prefix. Loading therefore
discards the cache from the tool list onward, once per load. After that the
cache hits again. That is cheaper than sending everything on every step. Loading
in the follow-up run (point 4) keeps the prefix stable across runs.

## Measured (30.09.2026, coder, `or-deepseek-flash`)

Paired, same task, local:

| | Tools | Schema | Prompt 1st call |
|---|---|---|---|
| without `deferred` | 52 | 46.8 KB | 16,793 tokens |
| with `deferred` (forge, coder_okf, coding_cli, datetime, sequential_thinking) | 20 | 27.0 KB | 11,581 tokens |

That is about 5,200 tokens less **per call** (−31 % of the first prompt). The
saving repeats with every step of the run.

Behavior in three runs:
- A task that needs a deferred tool (searching OKF): The model called
  `tool_search`, loaded two tools and used them. Loading cost one cache miss,
  after which the cache hit rate was 96 %.
- A task without need: nothing was loaded, correct answer in two steps.
- Continuation of the first session: The loaded tools were there from the first
  call, without `tool_search`, answer in one step.

Not measured is the error rate of tool selection over many runs. It is also
open whether stronger or weaker models overlook a deferred tool they would
need. Both will only show in operation.

## When to defer

Deferring pays off for tools that a typical run does **not** need and that bring
a lot of schema with them. One should not defer what almost every run needs
(reading files, shell): otherwise it costs a call and a cache miss on every run.
The sizes per tool come from a query on `data/message_debugger/debugger.db`
(`llm_requests.payload_json` → `tools`).
