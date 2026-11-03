# Plugins: architecture assessment, review and refactoring plan

**Scope:** `src/plugins/` (78 plugin folders, about 129,000 lines of production code without tests).
**Status:** Review done 2026-11; phase 0 (bugs) and the first extractions of phase 1 are merged; the rest is the plan.
**Related:** [Plugin architecture](_arch_plugin_architecture.md) · [Plugin authoring](plugin_authoring.md) ·
[Plugin hooks](plugin_hooks.md) · the verified quick reference `.claude/skills/plugin-authoring/`

---

## 1. Architecture assessment

### 1.1 What holds

- **One plugin contract.** Every plugin is a tool server (`agent_system/tools/`); tools come from `schema.yaml`,
  hooks from `schema.yaml hooks:`, web from `get_web_router`. Capabilities are detected from the object, so a plugin
  can be any mix without a type switch in the core.
- **The core already owns the dangerous parts.** Path containment (`utils/path_sandbox.PathSandbox`, `is_secret`),
  process confinement (`utils/process_sandbox`), atomic I/O (`utils/io`), parameter reading (`utils/params`), TLS
  (`llm/tls`), the key-follows-the-endpoint rule (`llm_common/api_keys`). Where a plugin uses them, the review found
  little to report: file_ops, terminal, mcp_client, mcp_gateway, forge's webhook, web_scraper's SSRF guard, media_ops.
- **Library plugins are the right shape for shared provider code.** `llm_common` (`type = ["library"]`) is imported by
  every LLM provider and declared in their `plugins = [...]`; a test keeps those lists equal to the imports.
- **Hardening is documented in place.** The large plugins (sub_agent_manager, stategraph, file_ops, terminal) explain
  each guard next to it, with the failure that motivated it.

### 1.2 What does not

| Problem | Where | Effect |
|---|---|---|
| **Core helpers exist but are not used** | No plugin imported `utils.io` before this review; `bool_param` had 11 divergent re-implementations; 8 plugins hand-roll path containment next to `PathSandbox` | Each copy drifts; the bugs in §2 are mostly copies that missed an edge case the core handles |
| **God files** | `context_engineer/compaction.py` 3.4k, `sub_agent_manager/server.py` 3.3k, `llm_openai_compat/httpx_client.py` 3.2k, `audio_ops/server.py` 2.5k, `lessons_learned/server.py` 2.2k, `context_engineer/hooks.py` 2.1k, `todo/server.py` 2.0k, `comfyui/server.py` 2.0k, `stategraph/engine/runner.py` 2.0k | Untestable units (`engineer_context` is one 570-line method; `httpx_client` has a 575- and a 940-line method with their own copies of payload building and retry); reviews miss interactions |
| **Plugins importing plugins' internals** | `coding_cli` → `mcp_client.connection._SSE_ALIASES`, `mcp_gateway.grants._check_path`; `tool_script` monkey-patches `script_interpreter`'s executor; `agent_eval` → `stategraph.engine.*`; `stategraph/engine/backend.py` → `stategraph/server.READ_ONLY_TOOLS` (engine above server); `llm_openrouter` → `llm_openai_compat.provider._timeout_config` | A private rename breaks another plugin; heavy import chains |
| **Implicit coupling by instance name and markers** | context_engineer/context_summarizer call `registry.get_server('context_usage_tracker')` by literal name; several plugins depend on `injected_by`, `prefixed_by`, `summary_mark`, `is_stale` written by others | A renamed instance silently degrades to estimates; nothing lists the contract |
| **Private core internals used by plugins** | `agent._session_tracker`, `agent._session_service`, `_current_tools_schema`, `_resolve_flat_tool_name`, `sessions._validate_session_id` | The core cannot change these without breaking plugins; a public run-handle API is missing |
| **Hook context rebuilt by hand** | The registry restores only `user_id`, `max_steps`, `final_call` after a hook returns a new `HookContext` | Any hook building a context field by field drops the rest (see context_summarizer, fixed) |
| **Hybrid wrapper boilerplate** | `memory`, `todo`, `sequential_thinking`, `cognitive_stack`, `lessons_learned`, `context_summarizer`, `context_engineer`, `sub_agent_manager`, `debate_forum` each write the same delegating `call`/`get_tools`/`call_tool`/`on_pre_llm_call`/`get_web_router` class | ~50 lines per plugin; each a place to forget one delegation (`get_security_config` was forgotten in debate_forum) |
| **Process-wide state keyed by model-chosen ids** | `cognitive_stack`, `sequential_thinking` (fixed); the pattern recurs wherever a server keeps a dict of "sessions" | One conversation reads another's state by naming the same id |

### 1.3 Target structure

```
agent_system/                       core: contracts + shared mechanisms
  utils/params.py                   as_bool / as_int / as_float / bool_param / flag_param      (done)
  utils/io.py                       atomic_write_text / atomic_write_json (+ bytes, retry)     (text fixed)
  utils/clip.py                     middle_cut / clip_value / file_fingerprint                 (done)
  utils/path_sandbox.py             PathSandbox (+ output(name) that mkdirs)                   (exists)
  llm/tls.py                        httpx_verify / network_ssl_verify                          (done)
  auth/agent_access.py              agent_allowed (+ agent_min_role)                           (done)
  llm/message_roles.py              field(), written_by_person(), last_turn_index()            (plan)
  llm/tool_pairs.py                 tool_call_units(messages)                                  (plan)
  core/agent_runs.py                drain_run(agent, ...) -> RunOutcome                        (plan)
  plugins/hybrid.py                 HybridPlugin(server, web, hooks)                           (plan)
plugins/llm_common/                 shared by the LLM providers only
  request_end.py                    ending_error                                               (done)
  http_status.py                    RETRYABLE_STATUS, retry_after_seconds                      (done)
  batch_http.py                     raise_for_batch_status, DateTimeEncoder                    (done)
  media.py / sse.py / usage.py      parse_data_url, iter_sse_data, openai_usage                (plan)
plugins/<name>/                     one concern per module; the server holds the tool handlers
```

Rule of thumb for where shared code goes: **used by plugins of different families → core; used by one family
(LLM providers) → that family's library plugin; used inside one plugin → a module of that plugin.**

---

## 2. Review results

Six parallel reviews (LLM providers; context and hook plugins; execution tools; media and external services;
orchestration; cross-plugin duplication). Every item below was checked against the code; the fixed ones have a
test that fails without the fix.

### 2.1 Fixed

| Severity | Plugin | Defect | Fix |
|---|---|---|---|
| High | llm_openai_compat | The batch backend sent the model entry's `api_key` -- an OpenRouter or gateway secret when `base_url` points there -- to api.openai.com | Only an OpenAI entry's own key, else `OPENAI_API_KEY` (`api_keys.targets_openai`) |
| High | image_compose | With no `output_directories` (the default) an absolute `output_path`/`spec_path` from the model overwrote any file the process could write | Writes stay inside the project root and the data directory; private files refused always |
| High | file_ops | The private-file check read a symlink's own name; `notes.md -> config/*.env` reached grep and the semantic index | The link target is checked with `is_secret` |
| High | debate_forum | The panel API (any signed-in user) read every channel incl. other users' direct messages, deleted them, and posted as any agent into running debates | `get_security_config` → admin under any instance name (per-user ownership: plan §3, P3) |
| Med-High | cognitive_stack, sequential_thinking | Model-chosen ids (`"main"`, `"plan"`) shared across conversations and users; a global idempotency cache dropped a second conversation's thought and answered success | State owned by the creating conversation; idempotency keys per conversation |
| Med-High | llm_gemini | `functionResponse.response` not always an object: a tool answering a JSON list/number made every later turn a 400 (HTTP client and batch) | `function_response_body` wraps non-objects |
| Med-High | llm_gemini (batch) | A dumped `ImageContent` (`image_url: None`) crashed on `None.startswith`; base64 `source` images were never sent | Both forms handled |
| Medium | sub_agent_manager, decision, godot, duckduckgo_search | `bool()` on model arguments: `use_advanced_model: "false"` ran the premium chain, `blocking: "false"` blocked | `as_bool` |
| Medium | debate_forum, tavily_search, n8n, sqlite_query, agent_watchdog, lessons_learned, forge, sequential_thinking, cognitive_stack | Divergent copies of bool/int coercion: `"null"` read as true, `"on"` as false, YAML `.inf` crashed `__init__` (OverflowError), `limit: "20.0"` failed | One implementation in `utils/params.py` |
| Medium | context_summarizer | Rebuilt `HookContext` by hand and dropped `tools_schema` and the cancellation token; project_instructions after it snapshotted no tool roots for the rest of the session | `dataclasses.replace` |
| Medium | lessons_learned | Booked every lesson on every LLM step: `application_count` × step count, synchronous SQLite on the event loop, errors swallowed | Once per (session, request, lesson), in a thread, failures logged |
| Medium | memory | `open('w')` from worker threads without a lock: parallel tool calls truncated each other's file into invalid JSON | Atomic write, one save per session at a time, snapshot on the loop, session id checked |
| Medium | basic_agent | Returned from inside the run's event loop; the run generator was closed by the garbage collector in another task while holding its session lock | Closed in `finally`, shielded |
| Medium | llm_anthropic, llm_openai_compat (batch) | `float(Retry-After)` raised ValueError on the HTTP-date form and hid the 429 | `retry_after_seconds` |
| Medium | llm_openai_compat (httpx) | Slept whatever Retry-After asked (3600 s, or `inf` forever) | Capped at 60 s |
| Medium | agent_eval | Admin-only only through the path rule of the instance name `agent_eval` | `get_security_config` → admin |
| Medium | llm_common (schema_sanitize) | Merging the first `anyOf` option sanitized its `properties` map as keywords; parameters named `format`/`title`/`pattern` vanished | The option is sanitized as a schema |
| Low-Med | web_scraper, weather | Read `system_config.ssl_verify`, which does not exist; `network.ssl_verify: false` had no effect | `llm.tls.network_ssl_verify` |
| Low-Med | ssh_control | Exit status `None` (channel closed without one) reported as exit 0 = success; machine store used a fixed temp name | -1; `atomic_write_text` |
| Low-Med | log_viewer | `tail` unbounded (`lines=10**7` returned the whole log into context), `readlines()` on the loop, errors without `status` | Capped, streamed in a thread, error shape |
| Low-Med | blender | A recv boundary inside a multi-byte character raised `UnicodeDecodeError` past the handler; the result clip never cut long strings inside a mapping | `except ValueError`; shared `clip_value` |
| Low | llm_router | `available_providers` always empty (dict check on model objects); a client per call never closed | Fixed both |
| Low | context_engineer | Fallback `"Continue with the task."` without `injected_by` counted as a person's turn | Marked |
| Low | script_interpreter | Import-time `sys.path.insert` of the repository root | Removed |
| Low | llm_ollama | `ollama_mode` compared case-sensitively for the unwired warning | Lower-cased first |
| Low | core `utils/io.atomic_write_text` | `tempfile` made every rewritten file 0600 | Unique temp name with a plain `open` |

### 2.2 Open (confirmed, not yet fixed) -- ordered by severity

| Sev. | Plugin | Defect (file:line at review time) | Proposed fix |
|---|---|---|---|
| High | llm_openai_compat | `httpx_client.py` retry loop: heal `continue`s (encrypted-reasoning, signature bypass, pin release, body-429) consume attempts; a heal on the last attempt mutates the session's messages and exits with an untyped `Exception` the fallback chain does not recognise | `while` loop that does not count heals; typed error after the loop (as `openai_responses_client` does) -- part of P2 split |
| Med-High | llm_openai_compat, llm_openai, llm_anthropic | A `multimodal_content` injection of a non-last parallel tool result lands between tool results (tool, user, tool): OpenAI 400; Anthropic breaks "tool_result first" | Buffer injections until the run of tool messages ends |
| Med-High | llm_gemini (batch) | Parallel tool responses not merged into one `Content`; list content for assistant text fails validation | Reuse `gemini_utils` converters (P2) |
| Med-High | audio_ops | CPU-heavy pydub/numpy work on the event loop (mix, overlay, envelope; `result += segment` quadratic) | `asyncio.to_thread` |
| Medium | ssh_control | Foreground and background output unbounded in memory and in the answer | Byte cap as in terminal's `_read_capped` |
| Medium | todo | Lock held only inside load and save, not across read-modify-write; an evicted session's save returns silently while the tool answers `created` | Lock across the mutation; raise instead of return |
| Medium | forge, coding_cli | `subprocess.run(timeout=)` for git kills only git: children leak on POSIX, `communicate()` can hang on Windows | Shared `run_git` with tree kill (`coding_cli/run.py` has the correct one) |
| Medium | context_engineer | Trailing-note walk stops at a `tool` message of tool_preload's injected pair: `compact_media_after_user_message` never fires on preload turns | Start below `_arrival_indices` / anchor on `opens_a_turn` |
| Medium | context_summarizer | Tool calls paired by id, last writer wins; repeated ids (Gemini batch `call_{name}_{index}`) orphan a tool result | Positional pairing (`compaction._tool_call_groups` → core `tool_pairs`) |
| Medium | web_scraper | `download` has no overall deadline (slow-drip server holds the call until 100 MB) | `asyncio.timeout` around the hop loop |
| Medium | comfyui | Attachments over the multimodal size limits reported as loaded; synchronous sqlite on the loop; connections not closed | Shared `attach_problem` check; `to_thread` + `closing` |
| Medium | context_engineer, context_summarizer | `get_latest`/`invalidate_session` of the usage tracker: synchronous SQLite with a 10 s busy timeout on the loop | `asyncio.to_thread` |
| Low-Med | audio_ops | `_temp_segment_*.wav` never cleaned and listed as user files; silencedetect regex misses negative/exponent values | `TEMP_PREFIX` + cleanup; `-?\d+(\.\d+)?(e[-+]?\d+)?` |
| Low-Med | web_scraper | 4xx/5xx pages cached as success | Do not cache `>= 400`, add `error` |
| Low | debate_forum | `_handed_over` grows when `session_end` is skipped | FIFO cap |
| Low | ask_user | Cancelled path without status end line or `status: error` | Standard error shape |
| Low | godot | `_drain` keeps all stdout/stderr of a running game | Ring buffer |
| Low | datetime | Business-day loops day by day (`days=10**7` blocks the loop) | Arithmetic or cap |
| Low | comfyui | Outputs named `{prefix}_{filename}` ignore `subfolder` and overwrite each other | Include the subfolder |
| Low | terminal | Background buffers: `list.pop(0)`, lines up to 64 KiB × 1000 | `deque(maxlen)` + byte budget |
| Low | sqlite_query | Column count not capped; database path relative to the cwd | Total-character cap; `PROJECT_ROOT` |
| Low | openai_api | Admin gate fails open when `app.state.config` is missing (forge, stategraph fail closed) | Fail closed |

### 2.3 Environment note

`web_scraper`: three tests (`test_long_text_is_paged_from_the_cached_copy`, `test_links_are_filtered_at_answer_time`,
`test_every_page_answer_is_scrubbed_errors_and_old_cache_included`) fail on a fresh Linux venv before and after these
changes (no `links` key: an optional HTML dependency is missing there). Not caused by this work.

---

## 3. Refactoring plan

Each step is behaviour-preserving unless it says otherwise, lands with the tests of the plugins it touches, and is
small enough to review on its own. Order: shared mechanism first, then its users, then the splits that become easy.

### P0 -- Bugs (done, §2.1); open bugs (§2.2) are scheduled into the phase that touches their code.

### P1 -- Shared helpers into the core / library plugins

| # | Extraction | Copies today | Status |
|---|---|---|---|
| 1.1 | `utils/params`: `as_bool`, `as_int`, `as_float`, `flag_param` | 13 plugins | **done** (remaining: `basic_operations._flag`, `image_compose` tri-state, `otel/settings`, `openai_api`, `context_engineer/compaction._coerce`, `audio_ops`/`ask_user`/`tool_approval`/`stategraph` float readers) |
| 1.2 | `llm_common`: `ending_error`, `retry_after_seconds`, `raise_for_batch_status`, `DateTimeEncoder` | 5 + 7 + 2 | **done** (remaining: `anthropic_client._retry_after`, `openai_client._seconds`, `system_one`, `n8n/client`) |
| 1.3 | `utils/clip`: `middle_cut`, `clip_value`, `file_fingerprint` | blender, godot (+ `_short` in terminal, ssh, skills, decision, image_compose; `_clip`/`_cap` in tool_script, forge, n8n, mcp_client) | **done** for blender/godot |
| 1.4 | `auth/agent_access.agent_allowed` | SAM, imported by agent_editor, stategraph | **done** |
| 1.5 | `llm/tls.network_ssl_verify` | web_scraper, weather | **done** (remaining: comfyui, weather `sources` use `httpx_verify`) |
| 1.6 | `utils/io`: `atomic_write_bytes`, `retry_sharing_violation`; migrate okf, json_store, coding_cli, agent_editor, agent_eval, file_checkpoints, audio_ops, llm_elevenlabs, context_engineer/atomic_json | 14 | text fixed, ssh migrated |
| 1.7 | `llm/message_roles`: public `field(msg, name, default)`, `written_by_person`, `last_turn_index`, `append_note_if_changed` | 10 plugins + 6 private core copies | open |
| 1.8 | `llm/tool_pairs.tool_call_units` (from `compaction._tool_call_groups`) for context_summarizer and message_validator | 3 semantics | open (fixes §2.2 summarizer pairing) |
| 1.9 | `llm/token_utils`: `extract_text_from_content(sep, include_files)`; `estimate_request_tokens`; `context_window_for(llm, agent)`; usage-tracker reading as a service, not `get_server('context_usage_tracker')` | 6 + 3 + 3 | open |
| 1.10 | `llm_common`: `media.parse_data_url`, `sse.iter_sse_data` (bytes, `data:` with or without space), `usage.openai_usage`, `errors.rate_limit_error`/`typed_error`, `timeouts.httpx_timeout_config` (public, for openrouter) | 8 + 4 + 4 + 6 | open (fixes Gemini U+2028 split) |
| 1.11 | `core/agent_runs.drain_run(...) -> RunOutcome`, always closing the generator shielded; `request_context.request_user_scope` | SAM, stategraph, basic_agent, openai_api | open |
| 1.12 | `auth`: `require_active_admin(request, auth_off=...)`, `json_object_body(request)` for plugin web handlers | user_management, agent_editor, stategraph, forge, openai_api, setup | open (fixes openai_api fail-open) |
| 1.13 | `plugins/hybrid.HybridPlugin` replacing the delegating wrappers; `SchemaBasedPluginHook.merged_config()` returning a copy (six hooks mutate `get_config()`'s internal dict today) | 9 + 6 | open |
| 1.14 | `utils/gitproc.run_git` + `utils/proctree.kill_tree` | forge, coding_cli ×2, terminal | open (fixes §2.2 git leak) |
| 1.15 | `tools/background.BackgroundRegistry` + recorded results | terminal ≈ ssh_control (7 near-identical pieces) | open (brings terminal's output cap to ssh) |
| 1.16 | `utils/media_types` (one ext → kind/MIME table) and `multimodal_tool_content.attach_problem` | media_ops, audio_ops, comfyui (tables disagree) | open |
| 1.17 | `utils/yaml_roundtrip` | agent_editor/store, stategraph/model/yamledit (already diverged) | open; removes stategraph → agent_editor import |

### P2 -- Split the god files (module boundaries from the review)

| File | Split into |
|---|---|
| `llm_openai_compat/httpx_client.py` | `openrouter_routing.py`, `chat_payload.py`, `body_errors.py`, `sse.py`; one `_request_events` generator for stream and non-stream like `openai_responses_client` (fixes the retry-loop bug) |
| `llm_gemini/gemini_utils.py` | conversion / stream detectors / thinking config / errors (errors → `llm_common`); batch uses the shared converters |
| `context_engineer/compaction.py` | `compaction_config.py`, `message_model.py`, `media.py` (`MediaCompactor`), `layers.py`, `archiving.py` |
| `context_engineer/hooks.py` | `session_registry.py`, `token_reading.py`, pure `gate.py` (`decide_gate(...)`), `restoration.py`, `tool_handlers.py` |
| `context_summarizer/hooks.py` | pure chunking/categorising functions; one `_record_outcome` |
| `sub_agent_manager/server.py` | `policy.py`, `slots.py` (`RunSlots`), `runs.py` (→ P1.11), `jobs.py` (`JobRegistry`), `ops_lifecycle.py`, `ops_query.py`, `ops_jobs.py`; one `_run_blocking()` for create/continue |
| `stategraph/service.py`, `engine/runner.py` | machines / runs / remote / queries / debug / fork_events / checks; replay / context / leases / manager; `READ_ONLY_TOOLS` down into the engine; public names for the kinds↔engine helpers |
| `audio_ops/server.py` | `paths.py`, `ffmpeg.py` (`probe_duration`, `detect_silences`), `envelope.py`; `__main__` drives the server |
| `comfyui/server.py` | `balancer.py`, `outputs.py`, `uploads.py`, `maintenance.py`; one `aiohttp` session per client; delete the unused `restart_service` |
| `coding_cli/server.py`, `todo/server.py`, `json_store/server.py`, `memory/server.py` | `RunStore`/`mcp_select`/`lifecycle`/`wake`; a shared `SessionJsonStore[T]` for todo and memory |
| `lessons_learned/server.py`, `image_compose/compositor.py`, `godot/server.py` | repository / ops / hooks; analysis / svg / text / spec; stderr / runner / bridge |

### P3 -- Contracts instead of internals

- A public run-handle API in the core for what plugins take from `agent._session_tracker` / `_session_service`.
- Public names for what other plugins import: `mcp_client.TRANSPORTS`, `mcp_gateway.grants.check_path`,
  `ScriptExecutor(seeds=..., should_abort=...)` for tool_script.
- The hook registry carries every `HookContext` field over (or rejects a context that lost one) -- the class of the
  context_summarizer bug.
- Document the message markers (`injected_by`, `prefixed_by`, `summary_mark`, `is_stale`) as a contract in
  `plugin_hooks.md`, with the plugins that write and read each.
- Per-user ownership for debate_forum channels, so the panel can be offered to users again.

### P4 -- Guards so it stays this way

- Extend the AST test that already checks status end lines to flag new private copies of the P1 helpers
  (`def _flag`, `def _as_whole`, `def _ending_error`, `def _short`, inline `tempfile` + `os.replace`).
- A test that every hybrid plugin delegates `get_security_config` (or uses `HybridPlugin`).
- A size budget per module (warn above 1,500 lines) for new code.

---

## 4. How to continue

Pick the next row of P1, move the helper, migrate every copy in the same change, keep the old private name as an
alias only where tests or other plugins reference it, run the touched plugins' tests (`src/plugins/<name>/tests`) and
`tests/pluginsystem/`, and tick the row here. Splits (P2) come after the helpers their code needs.
