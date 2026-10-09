# Architecture: FastAPI Application Layer

How the HTTP side of ScarabHive is built: the application factory, its
routes, how a run travels from a request to the agent and back, streaming,
cancellation and authentication. Every name below is a real function, route
or file; the code wins where this document and the code disagree. The running
server lists every route with its parameters at `/docs` (FastAPI's own page).

Related: [Agent system architecture](_arch_agent_system_architecture.md),
[Plugin architecture](_arch_plugin_architecture.md),
[CLI architecture](_arch_cli_architecture.md).

## 1. Overview

There is one application, built by `build_app(config_path=None)` in
`src/agent_system/app.py`. Every route comes from a router: the app's own --
`/run`, `/events`, the agent and chat routes, the status pages -- from
`api/*_routes.py`, the rest from the other routers in `api/` and `ui/` and from
the plugins' web routers.

| Feature | Where |
|---------|-------|
| Agent runs, answered as JSON or streamed as SSE | `POST /run` (`api/run_routes.py`), `GET`/`POST /events` (`api/event_routes.py`) |
| Markdown answers, drawn by the web chat and agent-cli | [Multi-format output](multi_format_output.md) |
| Authentication: JWT (header or cookie) and per-user API keys | `auth/`, `api/auth_endpoints.py` |
| Per-user sessions | `services/session_manager.py`, `services/session_service.py` |
| Cancelling a run by its request id | `POST /api/requests/{request_id}/cancel` |
| Panels of plugins in the web UI | `plugin_web_registry`, `ui/routes.py`, [Plugin architecture](_arch_plugin_architecture.md) |

## 2. Modules

### API and UI

| Module | Contents |
|--------|----------|
| `app.py` | `build_app` (configuration, logging, bootstrap, authentication, middleware, the lifespan, the routers) and `run()` |
| `api/app_context.py` | `AppContext`, what the app's own routes share (below); `parse_json_body` |
| `api/run_routes.py` | `POST /run` |
| `api/event_routes.py` | `GET`/`POST /events`, and the reconnect to a running job |
| `api/run_start.py` | What `/run` and `/events` share around a run: the request id check, the agent and LLM choice (`get_agent_with_overrides`), opening, naming and saving the session |
| `api/session_writes.py` | Holding a session (session presence, `claim_session`/`let_go`) and writing it beside the runs: appends, `/undo`'s cut, the file rewind |
| `api/run_control_routes.py` | `/api/requests/{request_id}/status` and `/cancel`, `/events/{request_id}/append`, `POST /sessions`, `/sessions/{session_id}/append`, the `force_optimize` routes |
| `api/agent_routes.py` | `/agents...`, `/llm/profiles`, `GET /admin/config` |
| `api/chat_routes.py` | `/chat/...` |
| `api/page_routes.py` | `/health`, `/`, `/login`, `/status`, `/status/meta`, `/favicon.ico` |
| `api/tool_routes.py`, `api/hook_routes.py` | `/tools/...`, `/hooks...` |
| `api/endpoints.py` | `/api/debug/messages`, `/api/debug/context-stats`, `/api/health`, `/api/version` |
| `api/session_endpoints.py` | `/api/sessions...` -- list, get, update, delete, restore, messages, hierarchy |
| `api/auth_endpoints.py` | `/auth/...` -- login, logout, refresh, me, register, API key, password reset |
| `api/admin_endpoints.py` | `/admin/...` -- users, active sessions, config reload, system |
| `api/debug_endpoints.py` | `/debug/health`; `/debug/memory...` (admin, `AGENT_ENABLE_MEMORY_PROFILING=1`); `/debug/profile...` (admin, `AGENT_ENABLE_PROFILING=1`) |
| `api/question_routes.py` | `question_router`: the `/answer` and `/pending` routes a plugin mounts when it asks the person (ask_user, tool_approval) |
| `api/dependencies.py` | `get_agent`, `get_config`, `get_session_manager`, `get_tool_registry` for `Depends` |
| `ui/routes.py` | `/ui/...`, the panel catalog `/api/ui/catalog`, help |

The session, auth and admin routers are included only when `auth.enabled` is
true. The app's own routers come after them, in the order their routes were
always registered: FastAPI matches in order, and `/agents/{name}/...` stays
before `/agents/debug/{name}/...`.

**`AppContext`.** `build_app` stores one per app in `app.state.context`; a
handler takes it as `ctx: AppContext = Depends(app_context)`. It holds the entry
agent, the endpoint security enforcer and two configurations, kept apart on
purpose: `ctx.config` is the one `build_app` started with -- the enforcer and the
middleware were built from it and a reload does not rebuild them, so the auth
checks (`enforce_endpoint_security`, `validate_llm_access`) and the agent role
gate (`gate_refuses`) judge by it -- and `ctx.live_config()` is the one the app
runs on now (`POST /admin/reload-config` replaces `app.state.config`): whatever
answers a question about the configuration (profiles, the LLM override, the
agent details, `/admin/config`) reads that one. The services shared across the
process -- the session service, the registry, the tool integration -- are not
in it: handlers read them from `app_state` at call time.

### Authentication (`auth/`)

`security.py` (passwords with bcrypt, JWTs, API key hashing),
`dependencies.py` (`get_current_user`, `require_admin`), `middleware.py`
(rate limiting, security audit), `enforcement.py` (`EndpointSecurityEnforcer`,
route security from `auth.endpoint_security`; `compile_endpoint_rules` and
`first_matching_rule` read those rules for it and for the
`EndpointSecurityMiddleware` alike), `remote_paths.py` (which paths
a client off loopback may reach), `session_access.py` (who
sees which session), `agent_access.py` (an agent's `min_role`), `database.py`
(the user store).

### Services (`services/`)

| Service | Role |
|---------|------|
| `InitializationService` | One bootstrap for API, `agent-cli` and `agent-run`: builds and starts the `Runtime`, creates `SessionManager`/`SessionService` lazily and injects the session service into the agents (`agent_injection`) |
| `ConfigService` | `load_config(config_path=None, force_reload=False)` -- delegates to `config.settings.load_settings` (includes, `${VAR}` expansion, models) |
| `SessionManager` | Session files, one directory per user; async, works on dicts: `create_session`, `load_session`, `save_session`, `list_sessions`, `delete_session`. The facade over two components it owns: `SessionIndex` (`session_index.py`, the per-user index partitions and the listings read from them) and `SessionCache` (`session_cache.py`, recent copies and what this process has seen of each file); `session_paths.py` holds the id and path rules |
| `SessionService` | What a run does with its session: `open_for_run`, `load_and_restore_session`, `save_session` |
| `SessionArchive` | Archiving old sessions (a periodic sweep started in the lifespan): which trees go and when; the archive's format on disk -- manifest under its lock, the zips -- is `ArchiveStore` (`session_archive_store.py`) |
| `BackgroundJobManager` | Every streamed run is a job: its event buffer, followers, reconnect, `cancel_job` |
| `ToolServerService` | Status of tool servers and tools (`GET /tools/status`, `agent-cli mcp list`, `status`, `test`) |
| `ToolService` | `list_tools` for `agent-cli mcp tools`; unused by the API |
| `AgentService` | A stub, not used by any route |
| `config_reload` | `POST /admin/reload-config` and `agent-cli reload` |
| `system_status` | What the System panel shows (`GET /admin/system`) and the commit the process started from |

The lifecycle of the tool servers (`initialize`, `shutdown`, `list_all_tools`)
is in `tools/integration.py` (`ToolServerIntegration`); which tools an agent
may call is decided by `ToolDiscoveryService` (`servers/agent/tool_discovery.py`),
and tool calls -- hooks, parallel execution -- run in `ToolExecutionManager`
(`servers/agent/components/tool_execution.py`).

## 3. Startup

`build_app(config_path=None) -> FastAPI`:

1. `ConfigService().load_config(config_path=...)` loads the configuration.
2. `InitializationService.bootstrap_and_inject()` builds and starts the
   `Runtime`, which bootstraps every configured server, and injects the
   session service into the agents. (Skipped only when an earlier `build_app`
   in the same process left a bootstrapped `ToolServerIntegration`.)
3. `app.state` gets `agent`, `tool_registry`, `config`, `runtime`,
   `config_service`, `config_path`, `auth_config` and `context` (the
   `AppContext`).
4. The routers are included: the API router, the UI router, the debug router;
   with authentication on also the auth, admin and session routers, plus CORS,
   rate limiting and the auth middleware; then the app's own routers
   (`api/*_routes.py`). The profiling middleware comes with
   `AGENT_ENABLE_PROFILING=1`. Last and outermost, `auth/remote_paths.install`:
   with `network.remote_paths` set, a client not on loopback gets 404 for every
   path not listed.

On Windows, `agent-api` (`own_console.api`) and `app.run()` first start the
server as a process of its own on a hidden console (`agent_system/own_console.py`)
and pass its output on to the terminal; the VS Code tasks start uvicorn the
same way. On the terminal's console, a console host that stopped answering froze
the whole server: starting a process with pipes, CPython asks the console,
holding the GIL, whether a pipe is a console. The server's process is not the
one that was started; it and what it starts die with that one (a job object
the server joins before it starts anything), and Ctrl+C reaches it as before.
What is meant to outlive the server -- a woken run (`session_presence`), a
coding run (`coding_cli`) -- starts through `own_console.popen_outliving`.

The lifespan (`custom_lifespan`, nested in `build_app`) sets up the executor
and runs `_init_mcp_for_app`: it starts the batch queue manager, runs
`initialize_tools()` (`ToolServerIntegration.initialize` -- its bootstrap is
skipped because the registry is already filled -- then plugin discovery,
plugin hooks, `start_all()`), injects the session service into the plugin
registry (`InitializationService.initialize_for_api(plugin_registry=...)`),
sets `app.state.session_manager` and `app.state.session_archive`, and mounts
the plugins' web routers and static files (`plugin_web_registry.apply_to_app`).
Then it starts the archive sweep and the job cleanup. Plugin routes exist only
after the lifespan has run -- a test sees them only inside
`with TestClient(app)`. On shutdown it stops the job cleanup, the batch queue
manager and the tool servers (`shutdown_tools`).

`agent-cli` and `agent-run` call `InitializationService.initialize_for_cli()`,
the same `bootstrap_and_inject()` without the HTTP parts.

## 4. Routes

The main routes. "auth" means the route exists only with `auth.enabled: true`.

| Area | Routes |
|------|--------|
| **Runs** | `POST /run`; `GET`/`POST /events` (SSE); `POST /events/{request_id}/append`; `POST /sessions/{session_id}/append`; `GET /api/requests/{request_id}/status`; `POST /api/requests/{request_id}/cancel` |
| **Agents and LLMs** | `GET /agents`; `GET /agents/{name}/tools`; `GET /agents/{name}/allowed-tools`; `GET /agents/debug/{name}/allowed-tools`, `GET /agents/debug/{name}/system-prompt` (admin); `GET /llm/profiles` |
| **Chat commands** | `/chat/commands`, `/chat/resolve`, `/chat/command`, `/chat/vars`, `/chat/undo`, `/chat/checkpoints`, `/chat/rewind`, `/chat/context`, `/chat/transcript`, `/chat/last_answer` |
| **Sessions** | `/api/sessions...` (auth); `POST /sessions`; `/sessions/{session_id}/force_optimize`, `/sessions/force_optimize` |
| **Auth** (auth) | `POST /auth/login`, `/auth/logout`, `/auth/refresh`, `/auth/register`; `GET`/`PATCH /auth/me`; `/auth/me/preferences`; `POST`/`DELETE /auth/api-key`; `/auth/password-reset...` |
| **Admin** | `/admin/users...`, `/admin/active-sessions` and `.../{request_id}/cancel`, `/admin/reload-config`, `/admin/system`, `/admin/security/audit` (all auth); `GET /admin/config` |
| **Status and tools** | `/status` (redirects to `/`), `/status/meta` (status bus metrics), `/health`, `/api/health`, `/api/version`, `GET /tools/status`, `GET /tools/cache/statistics`, `POST /tools/cache/invalidate` |
| **Hooks** | `/hooks...` |
| **Web UI** | `/` and `/login` (HTML), `/ui/...`, `/api/ui/catalog`, plugin panels under `/plugins/<instance>/` |
| **Debug** | `/api/debug/messages`, `/api/debug/context-stats`, `/debug/health`, `/debug/memory...`, `/debug/profile...` |

There is no HTTP route that executes a single tool; tools run inside an agent
run.

### Request bodies

**Error contract for body parsing:** the app's own routes that read a JSON
body (`POST /run`, `POST /events`, the append routes, the `/chat/...` POSTs)
parse it with `parse_json_body()` (`api/app_context.py`) and answer syntactically broken JSON with
**HTTP 400** `{"detail": "Invalid JSON body: could not be parsed"}`; a broken
multipart body sent to `/run` gets a 400 as well. Routes with a Pydantic body
model (auth, admin, sessions) answer it with FastAPI's 422.

**`POST /run`** takes JSON `{task, session_id, agent_name, llm_profile,
session_title, request_id, force}`, the same as query parameters, or
multipart (`task`, `files`, `attended`). Without files it waits for the run
and answers `{"task", "calls", "summary"}`, plus `errors`, `cancelled` or
`refused` when they apply. With files it streams the run as SSE.

**`GET /events`** takes `task`, `session_id`, `agent` (or `agent_name`),
`llm_profile`, `request_id`, `force`, `session_title`, `attended` as query
parameters (`task` is required); `POST /events` takes the same as JSON. `attended` says that a person watches
the stream and may be asked (see `request_context.set_run_attended`).

## 5. How a run travels

### 5.1 `POST /run`

1. `AppContext.enforce_endpoint_security` -- the route security of
   `auth.endpoint_security` (`EndpointSecurityEnforcer`) -- and
   `AppContext.validate_llm_access` (`auth.llm_security`: may this caller, an
   anonymous one say, start an LLM run at all).
2. `get_agent_with_overrides` (`api/run_start.py`) picks the agent; an unknown
   name answers 404 (`agent_not_found:<name>`).
3. `open_session_for_run` -> `SessionService.open_for_run` opens the session
   (403 for another user's).
4. `_mirror_run_as_job` registers the run as a job, so it can be followed and
   cancelled like a streamed one (409 if a job already runs under that
   request id).
5. `claim_session` (`api/session_writes.py`) holds the session through session presence
   (`core/session_presence/presence.py`, an OS lock next to the session file): a session
   another process runs (an open `agent-cli chat`, a woken run) or one deleted
   in this process answers 409; `force=true` runs it anyway, for the lock of a
   hung process -- a dead process's lock is released by the OS. A second run of
   the session inside this process is refused by the run itself (`refused` in
   the result).
6. `collect_final_result` (`servers/agent/result_utils.py`) drives
   `Agent.run_events` and collects calls, summary and errors.
7. `SessionService.save_session` stores the session; the JSON result goes back.

### 5.2 `GET`/`POST /events`

1. The same checks as above, then `_handle_events` (`api/event_routes.py`).
2. `BackgroundJobManager.create_job` runs `agent.run_events(...)` as a job.
   Status lines of tools and plugins (the status bus) join the run's own
   events through the `StatusEventForwarder`.
3. `_sse_lines` follows the job (`job.follow`) and writes each event with
   `_format_and_yield_event`, with keepalives in between.
4. A client that lost the stream reconnects with
   `GET /events?task=&request_id=<id>` (`task` is required, empty here),
   optionally `&catch_up=skip&seen=<n>`; it gets a `reconnect` event and the
   rest of the run. An id with no job left answers 404.

### 5.3 The stream

SSE lines: `:ok` and `:keepalive` comments, and `data: {...}` with one JSON
event each (on `/run` with files, an `event: <type>` line precedes each
`data:` line). `/events` sends `Cache-Control: no-cache` and
`X-Accel-Buffering: no`.
The server is uvicorn, HTTP/1.1.

| Event | Content |
|-------|---------|
| `start` | The run's `request_id` and `session_id` |
| `thinking`, `thinking_delta`, `thinking_complete`, `reasoning_delta` | What the model thinks, as it streams |
| `tool_call`, `tool_result`, `tool_error` | The tool calls of a step |
| `status` | A status line: `{type, server, request_id, message, phase, level, timestamp, meta, tree}` |
| `heartbeat` | `{step, max_steps}` (or `{step, timestamp}`) |
| `sub_run` | One event of a sub-agent's run below this one, wrapped: `{run_id, spawned_by, depth_level, agent, event}` |
| `final` | `{summary, content_format, usage}` -- the answer |
| `end`, `error`, `cancelled` | How the run ended |
| `reconnect` | Sent to a client that reattached by `request_id` |

## 6. Status bus

Tools and plugins report progress on the status bus (`tools/status.py`):
`await publish_status(server, message, request_id, phase, level, meta)`, or
`async with StatusScope(bus, server, request_id, start_msg, end_msg)`, which
sends the start and the end line. Phases: `START`, `PROGRESS`, `END`, `ERROR`.

`await status_bus.subscribe(server=None, request_id=None, maxsize=None)`
returns an `asyncio.Queue` (read with `await queue.get()`, released with
`unsubscribe(queue)`); the CLIs use it. The API does not subscribe per run: a
`StatusEventForwarder` puts the run's status lines into its event stream as
`status` events.

## 7. Cancellation

`POST /api/requests/{request_id}/cancel?force=true` calls
`BackgroundJobManager.cancel_job(request_id, force_timeout=5.0 if force else 0.0)`
-- with `force`, the run's task is cancelled after 5 s if it has not stopped
by itself. Admins also have `/admin/active-sessions/{request_id}/cancel`
(always with the 5 s force). Inside the run, the
cancellation token is keyed by the request id:
`get_cancellation_manager().create_token(request_id)` /
`get_token(request_id)`; code checks `token.is_cancelled` and may raise
`CancellationError(request_id, forced=False)` (`core/cancellation.py`). The
run ends with a `cancelled` event.

## 8. Authentication and authorization

### Modes

| Mode | Mechanism | Use |
|------|-----------|-----|
| **JWT** | `Authorization: Bearer <jwt>` or the HttpOnly `access_token` cookie set at login | Web UI, CLI |
| **API key** | `X-API-Key: <key>`, or `Authorization: Bearer <key>` (no dots: never a JWT) | Services, OpenAI-compatible clients (`openai_api` plugin) |
| **None** | `auth.enabled: false` | Local use |

`get_current_user` tries the Bearer header, then the cookie, then the API key.
`POST /auth/login` answers `{access_token, refresh_token, token_type,
expires_in}` and sets the cookie; `/auth/refresh` issues new tokens from the
refresh token (a JWT as well -- nothing is stored per token).

### Configuration

```yaml
# config/config.yaml
auth:
  enabled: true
  secret_key: ${JWT_SECRET}
  algorithm: HS256
  access_token_expire_minutes: 30
  refresh_token_expire_days: 30
```

API keys are per user, issued by `POST /auth/api-key` and stored hashed in the
user store -- there is no list of keys in the configuration.

### Who may do what

- **Routes:** `auth.endpoint_security` (app routes: `auth/enforcement.py`,
  `EndpointSecurityMiddleware`) and `auth.plugin_security` (plugin routes:
  `PluginEndpointSecurityEnforcer` in `plugins/web_adapter.py`) decide which
  roles reach a route; both are set in `config/security.yaml`, the only file
  that may set them. `require_admin` guards the admin router (role `admin`).
- **Sessions:** each user's sessions live in a directory of their own; the
  session routes serve only the caller's. Admins -- and everyone while
  authentication is off -- see all sessions in plugin views
  (`auth/session_access.py`).
- **Agents:** an agent's `metadata.min_role` gates who may run it, on every
  path (`auth/agent_access.py`; see
  [Configuration-Based Agents](config_based_agents.md#visibility-and-min_role)).

## 9. Errors

- Errors are mostly FastAPI's `HTTPException`: `{"detail": ...}`. Exceptions:
  `/tools/status`, `/tools/cache/...` and `/agents/debug/...` answer 200 with
  `{"error": ...}`; cancelling an unknown id answers 200
  `{"status": "not_found"}`.
- Status codes: 400 (broken body), 401/403 (authentication, role), 404
  (unknown agent or session), 409 (another process holds the session, or the
  request id is already running -- on `/events` and `/run` with files the
  session refusal arrives as an `error` event), 422
  (validation), 429 (rate limit, with authentication on), 503 (a service not
  initialised or the store unavailable).
- **LLM failures** do not become an HTTP status: they arrive as an `error`
  event, or in `errors` of the `/run` result. Before that, the agent moves along
  its profile chain; a rate limit, an exhausted quota or a rejected key also
  blocks that LLM for every agent in the process (see
  [Fallbacks](config_based_agents.md#fallbacks)). There is no response cache.
- An external tool server that fails at start logs a warning and the rest
  runs. A session that cannot be loaded starts as a new one -- except on a
  permission error, which is raised. A failing tool call becomes a
  `tool_error` event and the loop goes on.

## 10. Design decisions

- **FastAPI** for async I/O, request validation where models are used (auth,
  admin, sessions) and the generated `/docs`.
- **SSE instead of WebSockets:** a run's events go one way, server to client;
  SSE works through proxies and needs no protocol of its own. Input during a
  run goes through ordinary POSTs (`/events/{request_id}/append`).
- **JWT with refresh tokens:** stateless, nothing stored per token; the cookie
  serves the web UI, the header the CLI and clients.
- **Run logic in the route modules:** `/run` and `/events` call
  `Agent.run_events` themselves and share their steps through
  `api/run_start.py` and `api/session_writes.py`, with process state in the job
  manager and session presence. The `AgentService` that was meant to hold this
  logic is a stub; reuse between API and CLI happens one level lower, in
  `InitializationService`, `SessionService` and `Agent.run_events`.
- **Routers and an `AppContext`, not closures:** the routes were closures in
  `build_app` (some 3700 lines of it) and shared what they captured there. They
  are module-level handlers in `api/*_routes.py` now, and what they shared is
  per app on `app.state.context` -- per app, because a process (a test run) builds
  several apps, and module state would make one answer with another's agent or
  auth.

## 11. Related documents

- [Agent system architecture](_arch_agent_system_architecture.md)
- [Agent architecture](_arch_agent_architecture.md)
- [Plugin architecture](_arch_plugin_architecture.md)
- [CLI architecture](_arch_cli_architecture.md)
- [Multi-user authentication](multi_user_authentication.md)
- [Session management](session_management.md)
- [Tool execution](tool_execution.md)
