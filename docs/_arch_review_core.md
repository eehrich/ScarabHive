# Architecture Review: `src/agent_system`

**Document Type:** Architecture assessment
**Scope:** the core package `src/agent_system/` (~78k lines, 270 modules); the plugins only where the core leans on them
**Date:** 2026-11-03
**Companion:** [refactoring_plan.md](refactoring_plan.md) -- what was changed with this review and what is still open

---

## 1. Summary

The core is in better shape than its size suggests. The big decisions hold
up: every capability is a tool server, agents are configuration, hooks are the
one extension point into the run loop, and the config is stacked in layers with
a clear contract. Most modules document *why* they do what they do, and the
hard parts -- cancellation, session presence across processes, atomic writes
on Windows -- are deliberate and tested.

The weaknesses are of three kinds:

1. **Layering is enforced by lazy imports, not by structure.** `core/`,
   `utils/` and `tools/` (meant as the bottom layers) import from `servers/`,
   `services/`, `auth/` and `plugins/` inside functions. That hides cycles
   instead of removing them.
2. **A few god modules / god objects** carry too much: the `Agent` class (16
   mixins sharing one namespace), `api/session_endpoints.py`,
   `servers/agent/components/session_tracking.py`, `auth/middleware.py`,
   `plugins/discovery.py`, `hooks/registry.py`.
3. **Small helpers are re-implemented** in many places (atomic writes, the
   dict-or-attribute field read, request-id prefix matching, agent lookup by
   name, role ranking, session-error to HTTP mapping, truncation) and some of
   the copies have already drifted.

None of this blocks the system; all of it makes changes more expensive and
bugs more likely in the places where copies drift.

## 2. Package map and layering

```
            api/  app.py  ui/  cli_utils/  agent_cli.py  agent_run.py     presentation
                 │                                │
            services/   auth/   runtime.py   run_events.py               application
                 │
       servers/agent/ (Agent + mixins + components)   hooks/   skills/   domain
                 │
       llm/   tools/   plugins/   config/                                  domain infrastructure
                 │
       core/ (cancellation, request context, session presence)          base
       utils/  paths.py                                                   base
```

Intended direction: downward only. What the code does:

| From (lower) | Imports (higher) | Where |
|---|---|---|
| `core/` | `servers.agent.components.status_forwarding` | `core/run_questions.py` |
| `core/` | `services.session_paths`, `auth.agent_access`, `auth.security` | `core/session_presence/` |
| `core/` | `plugins.schema_loader` | `core/schema_base_mixin.py` |
| `tools/` | `servers.agent.server.Agent` | `tools/schema_mixin.py` |
| `plugins/` | `servers.agent.server.Agent` | `plugins/factory_utils.py` |
| `utils/` | `llm.models`, `llm.capabilities`, `auth.database` (private `_db`) | `utils/multimodal_processor.py`, `utils/path_sandbox.py` |
| `config/` | `plugins.catalog` | `config/inheritance.py` |
| `api/` | `plugins.agent_tasks` (for the user's identity) | `api/run_event_routes.py` |

All of these are lazy (inside a function) and therefore work, but each is a
cycle waiting to happen at import time. The remedy is the same every time:
move the shared constant or small function **down** (for example
`EPHEMERAL_SESSION_PREFIX`, `SESSION_ID_PATTERN` and `user_dir_of` into
`core/` or `paths.py`), or invert the call with a registered callback.

## 3. Assessment per area

### 3.1 Agent (`servers/agent/`)

- One `Agent` class assembled from ten mixins plus six in `llm_loop/`. The
  mixins share one flat namespace and depend on each other through implicit
  attribute contracts ("relies on `Agent.__init__` for ..."). It reads well
  per file, but it is one object.
- Components (`SessionTracker`, `ToolExecutionManager`, `ToolInvoker`,
  `HookIntegrationManager`) hold back-references to the agent and reach into
  private attributes; `SessionTracker` and `AgentRequestManager` share one
  private dict (`_active_requests`).
- Direction for the future: give the components explicit, narrow interfaces
  (a protocol per collaborator) instead of the agent back-reference; split
  `session_tracking.py` into the lock protocol, appends/watchers, tree queries
  and the message store.

### 3.2 API (`api/`, `app.py`)

- Moving the routes out of `build_app` into `api/*_routes.py` with a per-app
  `AppContext` was the right step. It is not finished: several routes still
  read the process-global `app_state.app_registry` instead of the app's own
  registry (`registry_of(request)`); the two agree only while one app exists.
- Routes carry domain logic (the live-conversation merge in
  `get_session`, title carrying across trackers) and call private
  `SessionManager` methods. `session_writes.py` is domain logic but raises
  `HTTPException`, so the CLI cannot reuse it.
- Identity is resolved four different ways (`AppContext`, `auth.dependencies`,
  `plugins.agent_tasks.user_of`, `question_routes.person`); with auth off one
  person is `"anonymous"` in one place and `"local"` in another.
- Admin-only routes (`/hooks*`, `/tools/*`, `/agents/debug/*`) rely solely on
  the rules in `config/security.yaml`; they do not check the role themselves.

### 3.3 Services and auth

- Sessions: `SessionManager` (facade, file writes) with `SessionIndex`,
  `SessionCache`, `session_paths`; `SessionArchive` + `ArchiveStore`;
  `SessionService` between the agent's tracker and the manager. Sound split;
  the leaks are callers using private manager methods.
- `auth/middleware.py` holds five unrelated middlewares; `auth.database._db`,
  the module globals of `auth/security.py`, and `SecurityAuditMiddleware._instance`
  are process-global state. The middleware and the route dependencies read the
  JWT secret from two places (`auth_config.secret_key` vs `security.SECRET_KEY`).
- Synchronous SQLite lookups run on the event loop for every authenticated
  request (twice: middleware and dependency).

### 3.4 LLM and config

- Provider abstraction is good: a manifest-driven seam table
  (`llm/registry.py`) and one `build_client`.
- Batch wiring was duplicated between `llm/factory.py` and
  `llm/batch/initialization.py` (two globals, two getters, two creation paths).
- Config: clean layers (`environment` → `layers` → `merging` → `inheritance`);
  the two merges (file stacking vs inheritance) are deliberately different and
  documented. `config` depends on the plugin catalog for type names, which
  pulls more of `plugins/` into every config load than needed.
- Large files that split naturally: `cache_key.py` (three jobs), `llm/models.py`
  (errors / messages / client base), `token_utils.py` (media durations vs
  estimators).

### 3.5 Plugins, tools, hooks, skills

- Discovery → adapter/registry → integration is a reasonable chain;
  `hooks/` is the cleanest package (no upward imports).
- `plugins/discovery.py` also did hook *registration*, duplicated almost line
  for line in `tools/integration.py` (now one path in
  `hooks/plugin_registration.py`).
- `plugins/__init__.py` imports the FastAPI-based adapters eagerly, which is why
  `llm/registry.py` keeps its own `plugin.toml` reader.
- The hook registry recomputes the topological order and deep-copies the
  messages once per hook on every call; caching the order per hook type is an
  easy win.

### 3.6 CLI, UI, utils

- `utils/` is half generic (`id`, `io`, `json_utils`, `params`, `redact`,
  `yaml_io`) and half domain code that belongs elsewhere: provider wire formats
  (`multimodal_tool_content`, `reasoning_artifacts`, `multimodal_processor`) →
  `llm/`; `tree_hierarchy` → `tools/`; `profiling`/`memory_profiling` → a
  diagnostics package; `path_sandbox`/`process_sandbox` → a sandbox package.
- `agent_run.py` re-implements most steps of `cli_utils/commands/run.py`, and
  the copies have drifted (stdout vs stderr for meta lines, title resolution).

## 4. Where the docs disagreed with the code

Corrected with this review: `run_events.md` (the `review_ready` end),
`cancellation_architecture.md` (the request-id scheme),
`llm_batch_api.md` (global provider timings, `max_retries` semantics),
`multi_user_authentication.md` (API key shape, `rate_limit_multiplier` not
applied), `session_archive.md` (no automatic index rebuild),
`session_management.md` (cache is not LRU), `_arch_cli_architecture.md`
(missing modules), `_arch_plugin_architecture.md` (`PLUGIN_NAME`/`register()`
override the folder name), `_arch_app_architecture.md` (module table).

## 5. Recommendations (in order)

1. **Finish the `AppContext` move**: one `lookup_agent(request, name)`, one
   identity resolver, no reads of `app_state.app_registry` from routes.
2. **Push shared constants down** to end the upward lazy imports (session-id
   pattern, user dir, request-id tree) and add an import-layer test that fails
   on new upward imports.
3. **Split the god modules** along the seams named above; keep re-exports so
   imports and test monkeypatch paths stay stable.
4. **One helper per concern** for the duplicated utilities (see the plan).
5. **Narrow interfaces for agent components** instead of back-references into
   the agent's private attributes.
6. **Fold `agent_run.py` onto the `run.py` step functions.**
