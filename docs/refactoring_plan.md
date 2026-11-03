# Refactoring Plan: `src/agent_system`

**Document Type:** Plan and change log
**Date:** 2026-11-03
**Basis:** [_arch_review_core.md](_arch_review_core.md) -- the assessment this plan acts on

The review looked at the whole core package, one area at a time (agent and
core; API; services and auth; LLM and config; plugins, tools, hooks and
skills; CLI, UI and utils). Every finding was checked against the code
before it was changed. Part 1 lists what changed with the review, Part 2
what is still open, ordered by value against risk.

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
| agent | Session presence busy-waited on the event loop | `hold`/`release` in a thread |
| agent | `llm_heartbeat_interval` < 0.1 s → `ZeroDivisionError` | `max(1, …)`; ≤ 0 disables |
| agent | Dead `CancelledError` branch in `collect_final_result` | Cancellation propagates explicitly |
| hooks | `begin`/`end` anchored nothing: `after: [end]` did not run last | Edges `begin→h→end`; today's order unchanged |
| hooks | A raising hook filter escaped `execute_hooks` | Own `try`, falls back to the default |
| hooks | `execute_hooks(timeout=)` was ignored | Explicit argument wins |
| plugins | Plugin schema looked up by instance name under the CWD (instances with a different type lost their hooks) | Looked up beside the factory's module |
| tools | Tool-cache listeners piled up per integration | Removed on `shutdown()` |
| skills | `$ARGUMENTS[0]` and `$ARGUMENTS[1]` were both the first argument; `$0` unsupported | Zero-based, as documented elsewhere |
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
| utils | **Sandbox:** a confined process could redirect git's config/hooks through a writable `.git/commondir` | `commondir` protected (Seatbelt, bwrap); a missing `.git/hooks` gets a read-only placeholder under bwrap |
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
| `dumps_lenient(obj)` | `utils/json_utils.py` | six lenient JSON dumps; dead `safe_serialize` |
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

All old import paths still work (re-exports).

### 1.4 Dead code removed

`PluginToolRegistry.register_plugin`, `LLMFactory`, the batch provider
identity normaliser, `json_utils.safe_serialize`, `LoopState.results` /
`StepState.tool_results`, the one-argument hook-filter branch, duplicate CLI
colour helpers. `utils/__init__.py` and `plugins/__init__.py` now load lazily
(importing `agent_system.utils.id` no longer loads the vector store; importing
`plugins.plugin_manifest` no longer loads FastAPI).

---

## Part 2 -- Open

### 2.1 Bugs and gaps that need a decision

| # | Item | Why open |
|---|---|---|
| O1 | Archive age prefers the index row's `updated_at`, which several metadata writers do not update | The index time is preferred on purpose (restored backups have fresh mtimes). Needs: those writers update the index row, or a separate "last conversation change" field. |
| O2 | An archive pass that extends a zip left from an unfinished pass can leave newer members out of sync | Needs per-member mtime in the zip manifest. |
| O3 | Hooks and tools resolve the user in opposite orders (`user_of`: session metadata first; `tool_user`: request owner first) | Tests pin the hook order; a policy hook could judge a different user than the tool runs as. Decide one rule. |
| O4 | `llm/capabilities.py` builds its registry from a bare `load_settings()` (default config path) | Initialise from the live config at bootstrap and on reload (`app.py`, `agent_run.py`, `cli_utils/commands/run.py`, `services/config_reload.py`). |
| O5 | Sync SQLite on the event loop for every authenticated request | Give the middleware a small dedicated executor; keep the token dependency synchronous (it relies on no await between check and write). |
| O6 | bwrap cannot keep a *missing* `.git/commondir` / `config.worktree` from being created | An empty placeholder breaks git. Option: refuse workspace-write when the repo lacks them, or bind `.git` read-only and re-bind only `objects`, `refs`, `logs`, `index`, `HEAD`. |
| O7 | Batch timings are per provider in config but global in the polling loop | Per-provider timing in the loop -- a change in production batch behaviour. |
| O8 | `anonymous_access.rate_limit_multiplier` is accepted but not applied | Implement or drop from the model. |
| O9 | Admin-only routes (`/hooks*`, `/tools/*`, `/agents/debug/*`) rely only on `config/security.yaml` | Add an in-route admin check as `debug_endpoints` has. |
| O10 | Live state is not dropped when a run ends | `GET /api/sessions/{id}` reads it after the run released its lock, until the job ends; drop it when the job ends. |
| O11 | `HookIntegrationManager._auto_sync_session_messages` never takes effect (its marker is cleared right after the hooks) | Remove together with its ~15 tests and the comment in the context summarizer plugin. |

### 2.2 Structure (recommended order)

1. **Push shared constants down** (`EPHEMERAL_SESSION_PREFIX`, `SESSION_ID_PATTERN`,
   `user_dir_of` → `core/` or `paths.py`) and add an import-layer test that
   fails on new upward imports from `core/`, `utils/`, `tools/`.
2. **One identity resolver for requests** (`auth/request_user.py`:
   `auth_enabled`, `signed_in_user`, `require_admin_user`) replacing the four
   ways in `api/` and `plugins/agent_tasks.py`; one user id with auth off.
3. **No private `SessionManager` calls from outside** (`_find_session_owner*`,
   `_session_id_exists_globally`, `_rebuild_index`): make the ones that are
   used public.
4. **`session_writes.py` free of HTTP** (domain errors, mapped in the routes),
   so the CLI can reuse it; `session_folders.py` → `services/`.
5. **`utils/` regrouped**: provider wire formats → `llm/`; `tree_hierarchy` →
   `tools/`; profiling → a diagnostics package; `path_sandbox`/`process_sandbox`
   → a sandbox package (`process_sandbox` itself split per backend).
6. **Agent components behind narrow protocols** instead of back-references
   into the agent's private attributes; `SessionTracker` and
   `AgentRequestManager` stop sharing `_active_requests`.
7. **Split further large modules**: `llm/cache_key.py` (auto key / breakpoint
   ladder / Anthropic marking), `llm/models.py` (errors / messages / client
   base), `llm/token_utils.py`, `plugins/discovery.py` (module loading /
   entry points), `ui/amigaguide.py`.
8. **Fold `agent_run.py` onto the `run.py` step functions** (listing, LLM
   override and session open are still duplicated).
9. **Hook registry performance**: cache the order per hook type; avoid one
   deep copy of the messages per hook.

### 2.3 Remaining duplication

- ISO timestamp parsing
  (`services/session_archive.py`, `cli_utils/session_archive_cli.py`,
  `auth/dependencies.py`, two plugins) → `utils/timeutil.parse_iso_utc`.
- Plugin manifest reading in `llm/registry.py` (now possible to share, since
  `plugins/__init__` is lazy).
- Hand-rolled atomic writes in `ui/help_index.py` (own retry window) and in
  several plugins.
- `run_events._cut` (strips trailing spaces before the ellipsis) vs `shorten`.
- Role ranking via `ROLE_HIERARCHY` in `plugins/web_adapter.py` and
  `ui/catalog.py`; user DB path in `first_admin.py` and `cli_utils/users.py`.
- `services/agent_service.py` (unused stub), `agent_injection.inject_dependencies_into_agents`,
  `enforcement.UserContext`: unused in production, still referenced by tests.
