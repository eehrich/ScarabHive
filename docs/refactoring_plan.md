# Refactoring Plan: `src/agent_system`

**Document Type:** Plan and change log
**Date:** 2026-11-03
**Basis:** [_arch_review_core.md](_arch_review_core.md) -- the assessment this plan acts on

The review looked at the whole core package, one area at a time (agent and
core; API; services and auth; LLM and config; plugins, tools, hooks and
skills; CLI, UI and utils). Every finding was checked against the code
before it was changed. Part 1 lists what changed with the review, Part 2
the second round on the decisions taken, Part 3 what is still open.

**Not taken yet, each waits for a decision:** zero-based `$N` in skills, the
sandbox's `.git` metadata (`commondir`, placeholders), admin checks inside the
routes on top of `config/security.yaml`. Session presence stays on the event
loop: its release runs in the run's last `finally`, under the client's cancel,
where an await would never reach it. The hook integration's auto-sync stays,
although nothing reads what it stages on the normal path.

---

## Part 1 -- Done

### 1.1 Bugs fixed

| Area | Bug | Fix |
|---|---|---|
| api | A refused or failing session claim in multipart `/run` and `/events` leaked the upload temp dir, the request-owner entry and the attended mark | Claim inside the `try/finally`; every early exit releases |
| api | `/chat/command` checked the owner on the entry agent's tracker while the command ran on another agent; `user_id` was `None` with auth off | Owner checked on the commanded agent's tracker; `viewer(user)` |
| api | Valid JSON that is not an object (`[]`) gave a 500 on `/events`, the append routes | `parse_json_object` → 400 |
| api | The run-event SSE subscribed outside its generator and could leak a subscriber queue | Subscribe inside the generator |
| api | `verify_session_owner` joined an unvalidated session id into a path (`../`), scanned the disk on the event loop and failed open on any error | Id validated (400), scan in a thread, a failing lookup refuses (503) |
| api | `POST /sessions` ran without auth and created ownerless sessions | Auth enforced, owner recorded |
| api | Upload files were written synchronously on the event loop | `asyncio.to_thread` |
| agent | A run cancelled while waiting for the session lock stayed registered as active forever | Unregister on `BaseException`, re-raise |
| agent | Pre/post-LLM hook tasks and pending tool tasks kept running (and billing) after the run was closed | Cancelled in `finally` |
| agent | Live state was FIFO, called LRU, and never dropped | Real LRU; dropped with a discarded session |
| agent | `llm_heartbeat_interval` < 0.1 s → `ZeroDivisionError` | `max(1, …)`; ≤ 0 disables |
| agent | Dead `CancelledError` branch in `collect_final_result` | Cancellation propagates explicitly |
| hooks | `begin`/`end` anchored nothing: `after: [end]` did not run last | Edges `begin→h→end`; one order changes, `run_steps.report_step` now runs last in `pre_tool_call` |
| hooks | A raising hook filter escaped `execute_hooks` | Own `try`, falls back to the default |
| hooks | `execute_hooks(timeout=)` was ignored | Explicit argument wins |
| plugins | Plugin schema looked up by instance name under the CWD (instances with a different type lost their hooks) | Looked up beside the factory's module |
| tools | Tool-cache listeners piled up per integration | Removed on `shutdown()` |
| llm | A string `error` in an Anthropic batch result aborted the whole job's results | Both shapes handled |
| llm | Requests without a result in a COMPLETED batch job waited until timeout (days) | Resolved with an error |
| llm | Cancelling one request cancelled every other request of the provider job | Only the own future; the job only when nobody else waits |
| llm | `init_batch_system` replaced the lazily created manager; clients stayed bound to the dead one | One global manager |
| llm | Batch job tracker rewrote its file in place (crash → orphaned, billing batches) | `atomic_write_json` |
| llm | Fire-and-forget immediate-submit task | Kept referenced, exception logged |
| llm | `'429' in str(error)` matched "14290 tokens" | Status code first, whole word otherwise |
| config | An absolute `includes:` glob crashed config loading | Absolute matches stay absolute |
| auth | Rate-limit sweep never removed anything | Idle IPs removed |
| auth | Concurrent registrations of `Alice` and `alice` both succeeded | `UNIQUE … COLLATE NOCASE` index; `IntegrityError` → `ValueError` |
| auth | The agent role gate opened the user DB at a different path than `UserDatabase` | Shared `user_db_path()` |
| services | An unreadable archive index was overwritten with one entry (all other archives unreachable) | Only a missing index starts empty; otherwise `ArchiveError` |
| services | A session resumed while its tree was being archived could be deleted | Busy check and mtimes re-checked before delete |
| services | `rename_session` was load + save without the session lock | Locked read-modify-write |
| services | Per-session locks were never pruned | `WeakValueDictionary` |
| services | Two hand-overs with one file name overwrote each other | Name claimed with `open(…, "xb")` |
| utils | Secrets readable on macOS through case variants (`CONFIG/LOCAL.ENV`) | Case-folded comparison on darwin |
| utils | Camera JPEGs (PIL `MPO`) sent as `image/mpo` | Mapped to real MIME subtypes |
| cli | `--list-sessions` and archive commands started the runtime and never shut it down; a broken default agent hid them | Run before the runtime; `try/finally: shut_down_runtime` |
| cli | `agent-run` warned about a save it never could do | Guarded |
| cli | `agent-cli --no-color users …` ignored the flag | Flags applied first; duplicate colour helpers removed |

Each fix comes with a regression test (see the commit messages for the files).

### 1.2 Shared code extracted

| Helper | Home | Replaces |
|---|---|---|
| `field_of(obj, name)` | `utils/fields.py` | four dict-or-attribute readers |
| `shorten(text, limit)` | `utils/text.py` | three truncation helpers |
| `dumps_lenient(obj)` | `utils/json_utils.py` | six lenient JSON dumps |
| `_atomic_replace` | `utils/io.py` | one mechanism under `atomic_write_text`/`atomic_write_json`; `ui/views.py` copy |
| `short_hash(text, n)` | `utils/id.py` | three sha256 fingerprints (byte-identical keys) |
| `in_request_tree(id, root)` | `core/request_context.py` | ~12 inline prefix matches |
| `conversation_messages`, `is_compaction_system_message` | `llm/message_roles.py` | three "conversation only" filters |
| `task_text(task)` | `llm/models.py` | two copies |
| `lookup_agent` / `registered_agent` | `api/app_context.py` | seven agent lookups, now on the app's own registry |
| `parse_json_object`, `errors_as_200` | `api/app_context.py` | inline body checks, seven try/log/return blocks |
| `session_errors_as_http` | `api/session_http.py` | seven exception→status mappings |
| `session_owner_agent` | `api/session_writes.py` | two copies |
| `role_rank`, `meets_role` | `auth/enforcement.py` | five role rankings |
| `USERNAME_RE` | `auth/models.py` | two drifted username checks |
| credential helpers | `auth/credentials.py` | header/bearer/cookie parsing in two middlewares |
| `SessionManager._mutate` | `services/session_manager.py` | five read-modify-write copies |
| `register_all_plugin_hooks` | `hooks/plugin_registration.py` | the hook-registration loop in `discovery.py` and `integration.py` |
| `parse_entrypoint` | `plugins/plugin_manifest.py` | two copies |
| `tool_defs_from_openai`, `strip_server_prefix` | `tools/base.py` | two copies each |
| `read_config_text` | `config/layers.py` | three ways to read a config file |
| `ENV_PLACEHOLDER` | `config/environment.py` | the agent editor's copy |
| one batch-manager global | `llm/batch/initialization.py` | two globals and creation paths |

### 1.3 Modules split

| Before | After |
|---|---|
| `api/session_endpoints.py` (1089) | `session_endpoints.py` (536) + `session_listing_routes.py` + `session_live.py` |
| `servers/agent/components/session_tracking.py` (960) | `session_tracking.py` (691) + `session_tree.py` + `session_locks.py` |
| `auth/middleware.py` (919) | package `auth/middleware/`: `endpoint_security`, `audit`, `rate_limit`, `headers`, `setup` |
| `hooks/registry.py` (791) | `registry.py` (647) + `ordering.py` |
| hook registration inside `plugins/discovery.py` | `hooks/plugin_registration.py` |

The old import paths still work (re-exports). What moved as module state is not re-exported as a copy: `plugins.discovery._BOOTSTRAPPED_HOOKS_REGISTERED`.

### 1.4 Dead code removed

The batch provider identity normaliser, `LoopState.results` /
`StepState.tool_results`, duplicate CLI colour helpers. Unused but kept, as
something a plugin may still call: `PluginToolRegistry.register_plugin`,
`LLMFactory`, `json_utils.safe_serialize`, the one-argument hook filter, the
hook integration's auto-sync to the session tracker. `utils/__init__.py` and `plugins/__init__.py` now load lazily
(importing `agent_system.utils.id` no longer loads the vector store; importing
`plugins.plugin_manifest` no longer loads FastAPI).

---

## Part 2 -- Second round (decisions taken)

| # | Item | Outcome |
|---|---|---|
| O1 | Archive age read a stale index row | Every `SessionManager` write that sets `updated_at` refreshes the index row (at most once per session per 30 s); the archive keeps preferring the index time |
| O3 | Hooks and tools resolved the user in opposite orders | One rule, `run_user`: the registered request owner first, session metadata only as fallback |
| O4 | The capability gate read the default config, not the process's | Built from the live config in `InitializationService`, rebuilt on config reload |
| O8 | `anonymous_access.rate_limit_multiplier` not applied | Applied to requests without credentials while auth and anonymous access are on |
| O10 | Live state never dropped at run end | Dropped when the session's last run and background job have ended |
| 2.2-1 | Upward imports from `core/` | Session-id helpers in `core/session_ids.py`; `tests/test_import_layering.py` fails on new upward imports from `core/`/`utils/` (ten documented exceptions) |
| 2.2-3 | Private `SessionManager` calls from outside | Public `find_session_owner(_async)`, `session_id_exists`, `rebuild_index`; core and plugins switched |
| 2.3 | Timestamps, manifest reader, role ranking, user DB path | `utils.timeutil.parse_iso_utc`; `plugin_manifest.read_manifest_table`; `role_rank`/`meets_role` and `user_db_path` everywhere in the core (the API included) |

## Part 3 -- Open

### 3.1 Bugs and gaps

| # | Item | Why open |
|---|---|---|
| O2 | An archive pass that extends a zip left from an unfinished pass can leave newer members out of sync | Needs per-member mtime in the zip manifest |
| O5 | Sync SQLite on the event loop for every authenticated request | Give the middleware a small dedicated executor; keep the token dependency synchronous (it relies on no await between check and write) |
| O7 | Batch timings are per provider in config but global in the polling loop | Per-provider timing in the loop -- a change in production batch behaviour |

### 3.2 Structure (recommended order)

1. **One identity resolver for requests** (`auth/request_user.py`:
   `auth_enabled`, `signed_in_user`, `require_admin_user`; `require_admin_viewer`
   moves there) replacing the four ways in `api/` and `plugins/agent_tasks.py`;
   one user id with auth off.
2. **The ten remaining upward imports** listed in `tests/test_import_layering.py`
   (callbacks instead of imports), and the same test for `tools/`.
3. **`session_writes.py` free of HTTP** (domain errors, mapped in the routes),
   so the CLI can reuse it; `session_folders.py` → `services/`;
   `_validate_session_id` callers (`api/chat_routes.py`, two plugins) onto
   `core.session_ids.validate_session_id`.
4. **`utils/` regrouped**: provider wire formats → `llm/`; `tree_hierarchy` →
   `tools/`; profiling → a diagnostics package; `path_sandbox`/`process_sandbox`
   → a sandbox package (`process_sandbox` itself split per backend).
5. **Agent components behind narrow protocols** instead of back-references
   into the agent's private attributes; `SessionTracker` and
   `AgentRequestManager` stop sharing `_active_requests`.
6. **Split further large modules**: `llm/cache_key.py` (auto key / breakpoint
   ladder / Anthropic marking), `llm/models.py` (errors / messages / client
   base), `llm/token_utils.py`, `plugins/discovery.py` (module loading /
   entry points), `ui/amigaguide.py`.
7. **Fold `agent_run.py` onto the `run.py` step functions** (listing, LLM
   override and session open are still duplicated).
8. **Hook registry performance**: cache the order per hook type; avoid one
   deep copy of the messages per hook.

### 3.3 Remaining duplication

- ISO timestamp parsing in two plugins → `utils.timeutil.parse_iso_utc`.
- Hand-rolled atomic writes in `ui/help_index.py` (own retry window) and in
  several plugins.
- `run_events._cut` (strips trailing spaces before the ellipsis) vs `shorten`.
- `services/agent_service.py` (unused stub), `agent_injection.inject_dependencies_into_agents`,
  `enforcement.UserContext`: unused in production, still referenced by tests.
- The lazy capability fallback (processes without bootstrap) still goes
  through `load_settings()` and its side effects.
