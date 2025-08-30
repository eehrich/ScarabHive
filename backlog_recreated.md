# Backlog

## Documentation and Guidelines

DO NOT MODIFY THIS FILE DIRECTLY. USE backlog TOOL AS INTERFACE. see docs/backlog_tool.md

The reason is to keep the syntax korrekt and check ids and validity.

### Legend

Legend: ✅ = done, ☐ = open, ❌ = failed, ⏳ = in progress/started/partially finished

### Notes

Note:
  - Each task includes `added` and `closed` dates where available. If `closed` is empty (—) the task is still open.
  - Maintain this file after each turn, update status
  - add new tasks and epics by your posposals to Epics - open
  - move finished Epics from 1. Epics - open to 2. Epics - closed, after user appoved (need to ask)
  - completely Proposals and new Idea add to 3. Ideas
  - Policy: Update `backlog.md` only for project-relevant changes (code, tests, docs, configuration). The assistant will not modify this file for routine conversational activity and will add an explicit backlog entry only when it makes or records a project change.
  - Status semantics guidance: use `reverted` or `rejected` when a change was declined or the team decided "do not want this change"; use `cancelled` (or `aborted`) when work was stopped because a new concept or direction superseded it. These statuses are canonical and will be recognized by the backlog tooling.

### Structure/Format

 Epic/Task Structure (must follow):

 if a field is mandatory and nothign to add, add "-"

- ☐ Task/Epic <uniqid 4 digits>: <title>
  - status: <status> (mandatory)
  - description: (optional)
    <multilinetext with detailed description>
  - added: <datetime> (mandatory)
  - closed: <datetime> (mandatory)
  - notes: (optional)
    <multiline text>

Additionally Epics can have tasks.
  - tasks: (mandatory)
    - <same task structure here>

### Sections

- Epics - open: contains all open Epics which are not started or in work
- Epics - finsihed: contains all Epics where all tasks are closed (done or aborted)
- Ideas: list of ideas for new tasks which are not listed in "open" yet.

## 1. Epics - open

- ☐ Epic 0001: Configuration / Managed-file model (partial)
  - status: open
  - description: Introduce manifest-based config and ensure managed-file writes only touch included files.
  - tasks:
    - ✅ Task 9002: Prevent overwriting master `config/agent.yaml` on enable/disable; write changes to a managed file instead
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Implemented master manifest (`includes`) handling and prefer writing to included file holding `mcp` (e.g., `mcp.yaml`).
        - CLI now resolves includes relative to the manifest and writes only to the managed include file.

    - ✅ Task 9003: Make managed-file writes atomic; remove backup/rotation for YAML-managed files
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Writes use a same-directory temporary file and `os.replace` for atomicity.
        - Removed automatic `.bak` rotation for managed YAML files per requested design.

    - ✅ Task 9004: Add `mcp.plugin_dirs` to config schema and ensure `AgentConfig`/`MCPConfig` include it
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Implemented `mcp.plugin_dirs` schema field and config loader resolves relative paths against the manifest.
        - CLI falls back to repository `plugins/` for discovery when configured dirs are empty; tests updated.

    - ✅ Task 9005: Move `mcp` block into an included managed file and add `config/mcp.yaml`
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Created `config/mcp.yaml` and updated `config/agent.yaml` to `includes: - mcp.yaml` so CLI managed writes target the included file.
        - Ensures master manifest stays small and safe (secrets remain in master).

    - ✅ Task 9006: Simplify plugin discovery and add environment override for plugin dirs
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Rewrote `discover_all_plugins()` to simplify directory normalization and prioritization: explicit config dirs -> env (`AGENT_PLUGIN_DIR`/`AGENT_PLUGIN_DIRS`) -> fallback `repo_root/plugins`.
        - Kept support for package-style plugins (`plugins/<name>/plugin.py`), legacy single-file plugins, entrypoints, and `plugin.yaml` metadata.
        - Normalization policy: Path objects resolve relative to cwd; string paths resolve relative to repo root. This keeps existing tests/UX stable.

    - ✅ Task 9007: Add unit test for `load_config()` include merge behavior
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Added `tests/test_loader_includes.py` to verify that included files (e.g. `mcp.yaml`) are merged into the loaded `AgentConfig` and included overrides take precedence.

    - ☐ Task 9008: Document include pattern in `README.md` and `developer_rules.md`
      - status: open
      - added: 2025-08-27
      - notes:
        - Recommend adding a short section explaining `includes:` usage, managed file practice, and env var overrides for plugin dirs.

    - ☐ Task 9009: Review and remove any remaining debug/logging cruft in plugin discovery
      - status: open
      - added: 2025-08-27
      - notes:
        - Consider further simplification or removal of past diagnostic code now that discovery is robust and covered by tests.

    - ✅ Task 9010: Add explicit per-role logging file config (logging.file_cli, logging.file_api)
      - status: done
      - added: 2025-08-28
      - closed: 2025-08-28
      - notes:
        - Added optional `file_cli` and `file_api` fields to `LoggingConfig` in `src/agent_system/config/models.py`.
        - Updated `src/agent_system/cli.py` and `src/agent_system/agent/interface_api.py` to prefer these explicit fields when present, with a backward-compatible fallback to the legacy single `logging.file` (the existing role-derived filename logic is retained if per-role fields are absent).
        - Ran full test suite after changes: 118 passed, 0 failed.

    - ☐ Task 9035: Replay: Propagate network.ssl_verify into LLM clients / OpenAI SDK
      - status: open
      - added: 2025-08-30

- ☐ Epic 0004: Plugins discovery & metadata
  - status: open
  - description: discovery via filesystem and entry-points, attach metadata to factories.
  - tasks:
    - ✅ Task 9013: (UX) Add optional pretty table output with `tabulate` and JSON output paths
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
    - ☐ Task 0106: Implement entry-point (setuptools) plugin discovery in addition to filesystem discovery and attach `plugin.yaml` metadata to discovered factories as `_plugin_metadata`
      - status: open
      - added: 2025-08-27
    - ✅ Task 0300: Finish remaining bootstrap & web_research_agent tests
      - status: done
      - added: 2025-08-28
      - closed: 2025-08-28
      - notes:
        - Fixed nested plugin discovery by reusing a previously-registered
        - `plugins` package __path__ when `importlib` cannot locate a spec.
        - Added safer plugin instantiation logging in bootstrap so factories
        - failing at runtime don't abort registration.
        - Verified locally: targeted and full test-suite runs pass after the
        - change (pytest run completed with all tests green).

- ☐ Epic 0005: Packaging / deps
  - status: open
  - description: ensure runtime deps and packaging steps are correct.
  - tasks:
    - ☐ Task 0107: Move `tabulate` into runtime dependencies (`pyproject.toml`) and document install steps
      - status: open
      - added: 2025-08-27

- ☐ Epic 0006: Safety & robustness
  - status: open
  - description: locking, backups, and write-failure tests.
  - tasks:
    - ☐ Task 0108: Consider adding locking to managed-file writes to avoid concurrent CLI runs stepping on each other
      - status: open
      - added: 2025-08-27
    - ☐ Task 0109: Add `backup_suffix` and `backup_rotate` fields to `MCPConfig` and include tests for backup rotation behavior (opt-in)
      - status: open
      - added: 2025-08-27
    - ☐ Task 0110: Add tests for atomic write failure modes and color helpers (explicit test list to maintain)
      - status: open
      - added: 2025-08-27

- ☐ Epic 0007: Docs / automation
  - status: open
  - description: README, hooks, and backlog tooling.
  - tasks:
    - ☐ Task 0111: Update `README.md` with managed-file behavior, examples for `mcp.managed_file`, and document that secrets remain in master `agent.yaml` (policy: do not remove secrets)
      - status: open
      - added: 2025-08-27
    - ☐ Task 0112: Add a Git hook or small script to remind/update `backlog.md` when opening PRs or completing tasks
      - status: open
      - added: 2025-08-27
    - ☐ Task 0113: Add `updated` timestamp metadata to tasks and track `updated` automatically when status changes
      - status: open
      - added: 2025-08-27

- ☐ Epic 0010: README, examples & onboarding
  - status: open
  - description: Improve documentation and onboarding for new contributors and users (PowerShell + venv examples, plugin author guide).
  - tasks:
    - ☐ Task 0117: Expand `README.md` with include/managed-file examples and policy notes
      - status: open
      - added: 2025-08-27
      - notes:
        - Add a short example showing `config/agent.yaml` with `includes: - mcp.yaml` and how `agent-cli plugins enable` writes to the included file.

    - ☐ Task 0118: Add a Quickstart section (venv activation on Windows PowerShell and Linux)
      - status: open
      - added: 2025-08-27
      - notes:
        - Include exact PowerShell commands (`. .venv/Scripts/Activate.ps1`) and `python -m pytest -q` example.

    - ☐ Task 0119: Create a Plugin Authoring guide and sample plugin template
      - status: open
      - added: 2025-08-27
      - notes:
        - Document `plugin.yaml` fields, minimal `plugin.py` factory pattern, and publishing notes.

- ☐ Epic 0011: Concurrency, locking & resilience
  - status: open
  - description: Ensure managed-file writes are robust under concurrent CLI invocations and handle partial failures gracefully.
  - tasks:
    - ☐ Task 0125: Add advisory/file locking for managed-file writes
      - status: open
      - added: 2025-08-27
      - notes:
        - Evaluate `filelock` or `fcntl`-based approaches; prefer a cross-platform advisory lock with small dependency or optional runtime.

    - ☐ Task 0126: Add retry/backoff and clear error messages on write failures
      - status: open
      - added: 2025-08-27
      - notes:
        - Implement a small retry loop for transient IO errors and tests simulating failures.

    - ☐ Task 0127: Integration test simulating concurrent `agent-cli` processes writing the same managed file
      - status: open
      - added: 2025-08-27
      - notes:
        - Use multiprocessing or subprocess with temp directories to validate locking and no-corruption guarantees.

- ☐ Epic 0012: Web UI / Dashboard for plugin management
  - status: open
  - description: Add a small HTTP API and lightweight web UI to view and manage plugins from the browser.
  - tasks:
    - ☐ Task 0130: Expose plugin list and enable/disable endpoints via FastAPI
      - status: open
      - added: 2025-08-27
      - notes:
        - Keep API minimal and re-use `agent_system` internals; secure endpoints in later iteration.

    - ☐ Task 0131: Prototype a tiny React/Vite front-end under `web/` that calls the API and shows plugin metadata
      - status: open
      - added: 2025-08-27
      - notes:
        - Minimal UI for listing plugins, toggling enable/disable, and viewing `plugin.yaml` metadata.

    - ☐ Task 0132: Add E2E test for API endpoints (smoke + permissions)
      - status: open
      - added: 2025-08-27
      - notes:
        - Use `requests` against a test server instance and validate JSON responses and managed-file updates.

- ☐ Epic 0013: Convert MCP servers to plugins
  - status: open
  - description: Convert built-in MCP server implementations into discoverable plugins (filesystem + entrypoint metadata) so servers can be enabled/disabled and maintained independently.
  - tasks:
    - ☐ Task 0133: Convert `web_scraper` server into a plugin
      - status: open
      - added: 2025-08-27
      - notes:
        - Provide `plugin.yaml`, adapt factory to plugin API, and add unit tests.

    - ✅ Task 9014: Convert `duckduckgo_search` server into a plugin
      - status: done
      - added: 2025-08-28
      - closed: 2025-08-28
      - notes:
        - Moved duckduckgo_search implementation from `src/agent_system/servers/duckduckgo_search` into `plugins/duckduckgo_search/`.
        - Added `plugin.py`, `server.py`, and `plugin.yaml` under `plugins/duckduckgo_search` and removed the original server files to avoid duplication.
        - Activated discovery via `plugins` directory (already present in `config/mcp.yaml` under `plugin_dirs`).
        - Added `tests/test_plugin_duckduckgo.py` asserting discovery and factory instantiation.
        - Ran test suite for the new tests: all passed.

    - ☐ Task 0134: Convert `web_research_agent` server into a plugin
      - status: open
      - added: 2025-08-27
      - notes:
        - Ensure dependencies are optional and documented.

    - ✅ Task 0135: Convert `duckduckgo_search` server into a plugin
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-28
      - notes:
        - Duplicate of Task 0133; conversion completed and shim removed (see Task 0133.1).
        - `plugins/duckduckgo_search` is present and discovered by the loader; tests validate discovery and factory instantiation.

    - ☐ Task 0136: Convert `google_search` server into a plugin
      - status: open
      - added: 2025-08-27
      - notes:

    - ☐ Task 0137: Convert `yahoo_finance` server into a plugin
      - status: open
      - added: 2025-08-27
      - notes:

    - ✅ Task 0138: Convert `weather` server into a plugin
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-28
      - notes:
        - Implementation exists under `src/plugins/weather` (plugin files and metadata present).
        - Loader discovers the `weather` plugin and tests validate discovery and factory instantiation.

    - ☐ Task 0139: Convert `twitter_search` server into a plugin
      - status: open
      - added: 2025-08-27
      - notes:

    - ✅ Task 0140: Convert `datetime` utilities/server into a plugin
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-28
      - notes:
        - Scaffold created and the `datetime` plugin is discoverable under `plugins/datetime`.
        - Loader changes avoiding stdlib collisions allow this plugin to be imported as `plugins.datetime` and tests validated discovery.

    - ✅ Task 0200: Fix plugin loader to avoid stdlib name collisions (e.g., `datetime`)
      - status: done
      - added: 2025-08-28
      - closed: 2025-08-28
      - notes:
        - Updated `src/agent_system/mcp/plugins.py` to load filesystem plugins under the `plugins.<name>` namespace and register a `plugins` package in `sys.modules` with appropriate `__path__` so absolute imports inside plugins resolve correctly and don't shadow stdlib modules.
        - Verified via `agent-cli plugins` that `datetime` plugin is discovered and listed.

    - ☐ Task 0201: (optional) Extend `plugins.__path__` to include all configured plugin directories
      - status: cancelled
      - added: 2025-08-28
      - closed: 2025-08-28
      - notes:
        - Decided not to implement a broad `__path__` append. Instead the loader now reuses a previously-registered `plugins` module __path__ (when present) and resolves configured `mcp.plugin_dirs` explicitly during discovery.
        - This approach avoids mutating package paths globally while still supporting editable installs and multiple discovery calls.

    - ☐ Task 0190: Convert plugin unit tests to async pytest style (pytest-asyncio)
      - status: open
      - added: 2025-08-27
      - notes:
        - Replace loop-based helpers with `async def` tests and `await` calls; ensure pytest-asyncio is in dev deps and CI runs these tests.

    - ⏳ Task 0191: Remove compatibility shim and update import sites to `plugins.<name>`
      - status: in progress
      - added: 2025-08-27
      - notes:
        - DuckDuckGo shim removed as part of Task 0133.1; other shims remain and should be migrated gradually.

    - ☐ Task 0192: Port additional MCP servers into `plugins/` and create shims for staged migration
      - status: open
      - added: 2025-08-27
      - notes:
        - Prioritize simple servers (`duckduckgo_search`, `yahoo_finance`, `weather`) and add plugin metadata files. Keep shims until CI is green.

    - ☐ Task 0193: Update CI to install/locate `plugins/` package console scripts and verify `mcp-<name>` entry points
      - status: open
      - added: 2025-08-27
      - notes:
        - Ensure `pyproject.toml` points scripts to `plugins.<name>.__main__:cli_main` or adjust packaging step; add CI job to run `mcp-<name> --help` for a sample set.

    - ⏳ Task 0194: Add integration test for plugin discovery & CLI (runs `agent-cli` and `mcp-<name>` subprocesses)
      - status: in progress
      - added: 2025-08-27
      - notes:
        - Work started: defined test plan and harness using temporary configs and subprocesses; implementation pending.

    - ☐ Task 0141: Convert `http_server` into a plugin (API adapter)
      - status: open
      - added: 2025-08-27
      - notes:

    - ☐ Task 0142: Convert `llm_router` into a plugin
      - status: open
      - added: 2025-08-27
      - notes:

    - ✅ Task 0143: Audit `agent` / `bootstrap.py` and create plugin task if appropriate
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-28
      - notes:
        - Performed an audit of `bootstrap.py` and adjusted plugin instantiation to log and continue on factory errors.
        - Ensured `bootstrap_servers()` uses `config.mcp.plugin_dirs` and added a minimal cwd `plugins/` fallback for developer/test UX.
        - Verified behavior via targeted tests (web_research_agent, sub-agent bootstrap); fixes merged and validated by local test runs.

- ☐ Epic 0014: Per-agent configuration and per-agent plugin control
  - status: open
  - added: 2025-08-27
  - description: Give each Agent an individual YAML configuration file and allow the CLI to configure which plugins are permitted for that agent, provided the plugin is enabled globally in `mcp.enabled_servers`.
  - tasks:
    - ☐ Task 0144: Add agent-level config schema and models
      - status: open
      - added: 2025-08-27
      - notes:
        - Define an AgentConfig model (e.g. in `src/agent_system/config/models.py`) supporting fields such as `name`, `description`, `plugins.allowed` (list), and other agent-specific settings. Ensure schema validation and defaults.

    - ☐ Task 0145: Implement agent config loader and includes/merge behavior
      - status: open
      - added: 2025-08-27
      - notes:
        - Implement loading logic to look for per-agent files under `config/agents/<agent>.yaml`, merge with master manifest includes, and respect existing managed-file semantics (write only to included managed files).

    - ☐ Task 0146: Add CLI subcommands for agent configuration
      - status: open
      - added: 2025-08-27
      - notes:
        - Extend `agent-cli` (`src/agent_system/cli.py`) with `agents` subcommands: `list`, `info`, `config`, `enable-plugin`, `disable-plugin`, `status`. Support `--dry-run`, `--yes`, and `--format json|table`. When enabling/disabling a plugin for an agent, the CLI must validate that the plugin is globally enabled in `mcp.enabled_servers` before persisting the per-agent config.

    - ☐ Task 0147: Persist per-agent config as managed files atomically
      - status: open
      - added: 2025-08-27
      - notes:
        - When the CLI updates per-agent allowed-plugins, write into `config/agents/<agent>.yaml` (or another managed include) using same-directory temp file + `os.replace`. Add tests to ensure atomicity and rollback safety.

    - ☐ Task 0148: Enforce per-agent plugin whitelist at agent bootstrap
      - status: open
      - added: 2025-08-27
      - notes:
        - Update agent creation/bootstrap code (e.g., `servers/bootstrap` or `servers/agent.server.Agent`) so the runtime plugin registry only exposes plugins that are both globally enabled and allowed by the agent's config. Add unit tests for enforcement.

    - ☐ Task 0149: Tests for loader, CLI, and runtime enforcement
      - status: open
      - added: 2025-08-27
      - notes:
        - Add `tests/test_agent_config.py`, `tests/test_cli_agents.py`, and integration tests that ensure an agent only loads permitted plugins. Use `BACKLOG_MD` fixture where needed and temporary config dirs.

    - ☐ Task 0150: Documentation and examples
      - status: open
      - added: 2025-08-27
      - notes:
        - Update `README.md` with examples for `config/agents/<agent>.yaml`, show CLI examples for per-agent plugin management, and document the rule that per-agent allowed lists cannot enable plugins that are disabled globally.

- ☐ Epic 0015: MCP protocol compatibility and adapters
  - status: open
  - added: 2025-08-27
  - description: Research existing open-source Model Context Protocol (MCP) specifications and popular open formats, select one or more target protocols, and implement adapter(s) so AgentSystem can interoperate with external MCP servers and common tooling.
  - tasks:
    - ✅ Task 0151: Research open-source MCP-like protocols and implementations
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
    - ☐ Task 0161: Create MCP (Anthropic) mapping and payload contract
      - status: open
      - added: 2025-08-27
      - notes:
        - Produce concrete mappings from AgentSystem internal calls to the MCP v1 payload shapes and endpoints (context publishing, tool calls, model request semantics). Add JSON Schema examples and sample payloads for publish_context and request_model.

    - ☐ Task 0162: Implement `AnthropicMCPAdapter`
      - status: open
      - added: 2025-08-27
      - notes:
        - Implement a concrete adapter `src/agent_system/mcp/adapters/anthropic.py` that subclasses the `MCPAdapter` interface and implements exact MCP v1 endpoint shapes, error handling, and retries.

    - ☐ Task 0163: Auth and secrets handling for remote MCP servers
      - status: open
      - added: 2025-08-27
      - notes:
        - Add secure handling for API keys, bearer tokens, and TLS verification flags. Support env-var placeholders in `config/mcp.yaml` and document recommended practices (use venv + env, or an external secret store).

    - ☐ Task 0164: Add integration smoke test against a reference MCP server
      - status: open
      - added: 2025-08-27
      - notes:
        - Create a smoke test that runs a small locally-started reference MCP server (or lightweight mock server) and validates publish_context/request_model round-trips. Add a docker-compose or test fixture for CI usage.

    - ☐ Task 0165: Update configuration schema and loader for MCP server definitions
      - status: open
      - added: 2025-08-27
      - notes:
        - Extend `config/mcp.yaml` schema with `remote_servers.<name>` entries including `adapter: anthropic|http`, `base_url`, `auth_env`, `timeout`, and optional `capabilities` mapping. Update settings loader and validation tests.

    - ☐ Task 0166: Add adapter registration and discovery
      - status: open
      - added: 2025-08-27
      - notes:
        - Implement runtime registration so adapters declared in config are available via the MCP registry. Allow selecting per-request target via `server` param and provide a default priority order (local -> configured remote -> generic HTTP fallback).

    - ☐ Task 0167: Documentation and examples for Anthropic MCP usage
      - status: open
      - added: 2025-08-27
      - notes:
        - Add `docs/mcp-anthropic.md` with examples for config, CLI usage, and an end-to-end example. Show how to run the smoke test and how to configure auth/env vars.

    - ☐ Task 0168: CI jobs and packaging notes for MCP adapters
      - status: open
      - added: 2025-08-27
      - notes:
        - Add CI steps to run adapter unit tests and the smoke test (use a mocked server or docker-compose). Document any new runtime dependencies in `pyproject.toml` and include install instructions for maintainers.

    - ☐ Task 0169: Backwards-compatibility and migration helpers
      - status: open
      - added: 2025-08-27
      - notes:
        - Provide a small migration guide and helper functions to map existing local MCP-style calls to the Anthropic MCP adapter. Add CLI `mcp migrate` dry-run mode to preview changes.

    - ☐ Task 0170: Performance, rate-limiting and retry defaults
      - status: open
      - added: 2025-08-27
      - notes:
        - Define sensible timeouts, retry/backoff defaults, and optional client-side rate-limiting for remote MCP requests. Add tests simulating transient failures.

    - ☐ Task 0152: Define an adapter interface and contract in code
      - status: open
      - added: 2025-08-27
      - notes:
        - Add an interface (e.g. `MCPAdapter` abstract base class) under `src/agent_system/mcp/adapters.py` describing methods for: publish_context(), request_model(), list_capabilities(), health_check(), auth handling, and timeouts.

    - ☐ Task 0153: Implement a minimal HTTP/JSON adapter (generic) that matches most MCP-like servers
      - status: open
      - added: 2025-08-27
      - notes:
        - Provide a generic `HttpMCPAdapter` that can be configured with endpoint, auth headers, and mapping rules. Include pluggable serializers for common payload shapes (JSON, MessagePack if needed).

    - ☐ Task 0154: Add configuration and discovery for external MCP servers
      - status: open
      - added: 2025-08-27
      - notes:
        - Extend `config/mcp.yaml` schema to include `adapters` and `remote_servers` entries. Add loader support to validate adapter config and secrets (use env var placeholders). Update `src/agent_system/config/models.py` and settings loader accordingly.

    - ☐ Task 0155: Implement an adapter for the selected target protocol(s)
      - status: open
      - added: 2025-08-27
      - notes:
        - Based on Task 0151 recommendation, implement one or more concrete adapters (e.g. `MCPv1Adapter`, `OpenMCPAdapter`) that translate AgentSystem calls to the external protocol.

    - ☐ Task 0156: Tests and integration smoke tests
      - status: open
      - added: 2025-08-27
      - notes:
        - Add unit tests for adapter interface and concrete adapters and an integration smoke test that runs against a local or dockerized MCP test server (use test fixtures and `BACKLOG_MD` override where needed).

    - ☐ Task 0157: Runtime integration and graceful fallback
      - status: open
      - added: 2025-08-27
      - notes:
        - Integrate adapters into bootstrap paths so components can route requests to local servers or remote MCP servers. Provide clear fallback to in-process handling when remote servers are unavailable.

    - ☐ Task 0158: Security, auth and rate-limiting guidance
      - status: open
      - added: 2025-08-27
      - notes:
        - Define secret handling policies (env vars, vault hooks), TLS/HTTPS recommendations, and optional rate-limiting/retry defaults for remote MCP calls. Add tests for missing/invalid auth.

    - ☐ Task 0159: Documentation, migration guide and examples
      - status: open
      - added: 2025-08-27
      - notes:
        - Update `README.md`, `docs/` and add an `examples/mcp-adapter.md` explaining how to configure and run an external MCP server with AgentSystem and how to author adapters.

    - ☐ Task 0160: CI, packaging and deployment notes
      - status: open
      - added: 2025-08-27
      - notes:
        - Add CI jobs for adapter tests and the smoke test (use an ephemeral test server or docker-compose). Document packaging changes if new runtime dependencies are introduced.

- ☐ Epic 0016: Implement "Tavily" websearch MCP plugin
  - status: open
  - added: 2025-08-27
  - description: Create an MCP-compatible plugin implementing websearch via the Tavily service (or a Tavily-compatible API), packaged as a discoverable plugin so it can be enabled/disabled per the project's plugin model.
  - tasks:
    - ✅ Task 0171: Research Tavily API and license compatibility
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Summary of findings (sources: Tavily docs, Tavily SDKs, community integrations)

    - ☐ Task 0172: Design plugin interface and metadata (`plugin.yaml`)
      - status: open
      - added: 2025-08-27
      - notes:
        - Define `plugin.yaml` fields (name, summary, homepage, authors, capabilities) and the plugin factory API expected by AgentSystem for MCP servers.

    - ☐ Task 0173: Implement Tavily MCP plugin scaffold
      - status: open
      - added: 2025-08-27
      - notes:
        - Add `plugins/tavily_websearch/plugin.py` and `plugin.yaml`. Implement adapter logic to map MCP requests to Tavily HTTP calls and map responses back to MCP response shapes.

    - ☐ Task 0174: Auth handling and rate-limit/backoff
      - status: open
      - added: 2025-08-27
      - notes:
        - Support API keys via env vars, implement exponential backoff and respectful rate-limiting. Add configurable retry counts and timeouts in `plugin.yaml` sample.

    - ☐ Task 0175: Tests and integration examples
      - status: open
      - added: 2025-08-27
      - notes:
        - Add unit tests mocking Tavily endpoints and an integration example (using a mocked local HTTP server) demonstrating discovery, enable/disable, and a basic search round-trip.

    - ☐ Task 0176: Documentation and usage snippets
      - status: open
      - added: 2025-08-27
      - notes:
        - Add examples to `docs/` and `README.md` showing how to enable the plugin, configure API keys, and use via `agent-cli plugins` and the MCP adapter.

- ☐ Epic 0017: MCP server status & progress interface (CLI + Web UI)
  - status: open
  - added: 2025-08-27
  - description: Add a lightweight, standardized status/progress reporting interface so MCP servers can emit short human-readable status updates (e.g. "searching the web for 'tavily'"), and expose these in the CLI and Web UI in real-time or near-real-time.
  - tasks:
    - ☐ Task 0177: Define status contract and API
      - status: open
      - added: 2025-08-27
      - notes:
        - Research summary and recommended schema/approach

    - ☐ Task 0178: Extend adapter/interface to emit and subscribe to statuses
      - status: open
      - added: 2025-08-27
      - notes:
        - Update `MCPAdapter` contract to include `publish_status()` and `subscribe_status()` hooks (or provide a small status bus wrapper) so both local and remote adapters can forward status events.

    - ☐ Task 0179: Implement status event bus component
      - status: open
      - added: 2025-08-27
      - notes:
        - Add `src/agent_system/mcp/status.py` with a lightweight pub/sub (async-friendly) broadcaster that adapters, servers and CLI/web backends can register to. Support filtering by `server` and `request_id`.

    - ☐ Task 0180: CLI integration for live status output
      - status: open
      - added: 2025-08-27
      - notes:
        - Add `agents status` or integrate into existing `run`/`plugins` flows to show short statuses; support `--follow` for streaming, `--format json|short` and `--no-color` compatibility. Ensure non-interactive tests remain deterministic (allow `--no-stream` fallback).

    - ☐ Task 0181: Web UI streaming endpoint and front-end hooks
      - status: open
      - added: 2025-08-27
      - notes:
        - Add server-side SSE/WebSocket endpoint to stream status updates to the UI; update the web UI to show per-agent/per-request statuses (compact toast or console area). Ensure CORS/auth considerations are documented.

    - ☐ Task 0182: Update MCP servers/plugins to emit statuses
      - status: open
      - added: 2025-08-27
      - notes:
        - Update representative servers (e.g., `web_scraper`, `tavily_websearch` plugin) to emit status events at meaningful points (start search, fetching, parsing, done/error). Add examples in plugin templates.

    - ☐ Task 0183: Tests and integration scenarios
      - status: open
      - added: 2025-08-27
      - notes:
        - Unit tests for status contract, event bus, CLI streaming mode, and front-end smoke tests (mock server). Ensure tests can run deterministically by supporting mocked/buffered status events.

    - ☐ Task 0184: Privacy, UX and performance guidance
      - status: open
      - added: 2025-08-27
      - notes:
        - Document what statuses are allowed (avoid PII), opt-in flags for verbose streaming, rate-limits for status events, and guidance on retention/visibility. Add examples to `README.md` and `docs/`.

    - ✅ Task 0185: Implement SSE endpoint and server bridge
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - SSE chosen as the primary streaming transport and is already implemented in the project's Web API (use existing `/mcp/status/stream`-style endpoint). Proceed to wire the in-process status bus to that SSE bridge and enable CLI consumers to subscribe.

    - ☐ Task 0188: Add request tracing / correlation support
      - status: open
      - added: 2025-08-27
      - notes:
        - Add `request_id` assignment helpers and `traceparent` propagation to status events. Integrate with existing logging to correlate log lines and status events; include simple examples in docs.

- ☐ Epic 0009: Tooling: Ruff & Mypy updates
  - status: open
  - notes:
    - Files added:
    - `src/plugins/web_scraper/server.py` (async `WebScraperServer` with httpx/urllib fallback, BeautifulSoup parsing, structured table/form/list extraction, and `_clean_text` normalization).
    - `src/plugins/web_scraper/plugin.py` (exports `PLUGIN_NAME` and `PLUGIN_FACTORY`, lazy imports, attaches `_plugin_metadata`).
    - `src/plugins/web_scraper/__main__.py` (CLI shim; lazy imports to avoid heavy deps at import time).
    - `src/plugins/web_scraper/plugin.yaml` (metadata: name, version 0.1.0, author).
    - `src/plugins/web_scraper/__init__.py` (package marker).
    - `tests/test_plugin_web_scraper.py` (discovery + instantiation test).
    - Legacy files removed:
    - `src/agent_system/servers/web_scraper/server.py`
    - `src/agent_system/servers/web_scraper/__main__.py`
    - Commits:
    - "feat: migrate web_scraper to plugin (add server, plugin, CLI shim, metadata)"
    - "chore: remove legacy web_scraper server (migrated to plugin)"
    - Verification:
    - Ran focused test: `pytest tests/test_plugin_web_scraper.py` -> pass.
    - Full test suite run: `pytest -q` -> 126 passed, 0 failed.

    - Moved duckduckgo_search implementation from `src/agent_system/servers/duckduckgo_search` into `plugins/duckduckgo_search/`.
    - Added `plugin.py`, `server.py`, and `plugin.yaml` under `plugins/duckduckgo_search` and removed the original server files to avoid duplication.
    - Activated discovery via `plugins` directory (already present in `config/mcp.yaml` under `plugin_dirs`).
    - Added `tests/test_plugin_duckduckgo.py` asserting discovery and factory instantiation.
    - Ran test suite for the new tests: all passed.

  - tasks:
    - ☐ Task 9023: Run Mypy and record findings
      - status: open
      - added: 2025-08-29
      - notes:
        - Mypy run: `.venv/Scripts/python.exe -m mypy src --show-traceback` returned 30 type errors in 11 files. Example files: `src/plugins/datetime/server.py`, `src/scripts/backlog_tool/values.py`, `src/agent_system/mcp/plugins.py`, `src/agent_system/utils/prompt_renderer.py`, `src/plugins/duckduckgo_search/server.py`, `src/plugins/web_scraper/server.py`, `src/agent_system/servers/http_server.py`, `src/agent_system/llm/clients.py`, `src/agent_system/servers/agent/server.py`, `src/agent_system/servers/web_research_agent/server.py`, `src/agent_system/cli.py`.
        - Left for triage: types need fixes (unions, missing attributes, unreachable code). Suggested next steps: triage files by frequency of usage, add minimal type hints, and run Mypy iteratively.

- ☐ Epic 0018: Backlog maintenance tool
  - status: open
  - description: Provide a cross-platform command-line tool and library (`backlog.exe` / `backlog` CLI) to safely add/update/move tasks and epics in `backlog.md`, run validation, and provide automated scaffolding for new tasks and epics.
  - notes:

  - tasks:
    - ☐ Task 0189: Design CLI and data model for backlog operations
      - status: open
      - added: 2025-08-28
    - ☐ Task 0199: Progress update and immediate next steps
      - status: open
      - added: 2025-08-28
    - ☐ Task 9024: Implement robust Markdown parser & safe writer library
      - status: open
      - added: 2025-08-28
    - ☐ Task 9025: Implement core CLI commands and operations
      - status: open
      - added: 2025-08-28
    - ☐ Task 0019: Add validation rules and `validate` command
      - status: open
      - added: 2025-08-28
    - ☐ Task 0020: Add tests (unit + integration) and CI job
      - status: open
      - added: 2025-08-28
    - ☐ Task 0021: Implement safety features: backups, dry-run, undo
      - status: open
      - added: 2025-08-28
    - ☐ Task 0195: CLI UX, docs, and examples
      - status: open
      - added: 2025-08-28
    - ☐ Task 0196: Packaging and `backlog.exe` build target
      - status: open
      - added: 2025-08-28
    - ☐ Task 0197: Git hooks and integration
      - status: open
      - added: 2025-08-28
    - ☐ Task 9026: Implement `backlog validate` rules and CLI
      - status: open
      - added: 2025-08-29
    - ☐ Task 9027: Add auto-fix mode for simple issues (`fix-format --write`)
      - status: open
      - added: 2025-08-29
    - ☐ Task 9028: Add unit + integration tests for validator and fixer
      - status: open
      - added: 2025-08-29
    - ☐ Task 9029: CI job to run `backlog validate` and `pytest` on PRs
      - status: open
      - added: 2025-08-29
    - ☐ Task 0198: Accessibility & safety review
      - status: open
      - added: 2025-08-28

- ☐ Epic 0002: CLI UX & features
  - status: open
  - notes:

  - tasks:
    - ✅ Task 9011: Add optional pretty table output with `tabulate` and JSON output paths
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - `tabulate` is imported optionally; when present, CLI uses it for table formatting.
        - CLI supports `--format json` for machine-friendly output.

    - ☐ Task 0101: Explicitly ensure CLI `plugins` subcommands cover flags and behavior: `list`, `info`, `enable`, `disable`, `search`, `status` with `--format json|table`, `--raw`, `--yes`, `--dry-run` (acceptance: documented and covered by tests)
      - status: open
      - added: 2025-08-27
      - notes:

    - ☐ Task 0102: Provide a CLI command to view or edit the managed file safely (preview + apply)
      - status: open
      - added: 2025-08-27
      - notes:

    - ☐ Task 0103: Add and document global color flags `--color`/`--no-color`, and keep `_supports_color()` / `_colorize()` helpers with tests
      - status: open
      - added: 2025-08-27
      - notes:

    - ✅ Task 0202: CLI: stream MCP-server calls/results live and pretty-print final result
      - status: done
      - added: 2025-08-28
      - closed: 2025-08-28
      - notes:
        - Implemented live streaming in `agent-cli` so MCP CALL and MCP RESULT events are printed as they occur in a human-readable, colorized format.
        - Added `--no-stream` global flag to disable live printing and preserve legacy behavior.
        - Final output now uses a pretty-printer that prints tool calls first and a colored Summary at the end; `--verbose` still prints raw JSON for debugging.

    - ✅ Task 0203: CLI: add `--raw` flag and tests for streaming vs raw output
      - status: done
      - added: 2025-08-28
      - closed: 2025-08-28
      - notes:
        - Implemented `--raw` global flag to force raw JSON output (works with `--no-stream`).
        - Added unit tests (`tests/test_cli_streaming_raw.py`) that validate both streaming human-readable output and `--no-stream --raw` JSON output.
        - Fixed test harness capture issues (flushing output, forwarding prelim flags) so tests pass under pytest.

    - ✅ Task 0205: Move ad-hoc debug scripts to `tools/debug/` and ignore them
      - status: done
      - added: 2025-08-28
      - closed: 2025-08-28
      - notes:
        - Moved several ad-hoc debug helpers from `tests/` into `tools/debug/` and added `tools/debug/` to `.gitignore` to avoid accidental commits.
        - Removed tracked copies from `tests/` and committed the cleanup.

    - ✅ Task 0206: Branch and merge `cli/streaming-plugins-fixes`
      - status: done
      - added: 2025-08-28
      - closed: 2025-08-28
      - notes:
        - Created branch `cli/streaming-plugins-fixes`, pushed to remote and merged into `main`.
        - Full test suite ran after merge: 118 passed, 0 failed.

    - ☐ Task 0204: CLI: suppress assistant interim content during streaming (optional)
      - status: open
      - added: 2025-08-28
      - notes:
        - Some assistant messages include human summaries before the final event; consider suppressing or filtering assistant content during streaming so only MCP events and the final summary are shown.
        - Low-risk approach: make suppression opt-in via `--suppress-assistant` flag or enable by default with clear documentation.

- ☐ Epic 0003: Tests & CI
  - status: open
  - notes:

  - tasks:
    - ✅ Task 9012: Add tests for `plugins info` output and managed-file writes
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Tests updated to use master manifest + included `mcp.yaml` and validate managed-file writes.
        - Full test suite run: all tests passing.

    - ☐ Task 0104: Add integration test that runs `agent-cli plugins enable/disable` end-to-end with temporary configs
      - status: open
      - added: 2025-08-27
      - notes:

    - ☐ Task 0105: Add a small pre-check that validates `agent_system.cli` imports cleanly before running full test suite
      - status: open
      - added: 2025-08-27
      - notes:

## 2. Epics - finished

- ✅ Epic 0008: Backlog (backlog maintenance & validation)
  - status: done
  - closed: 2025-08-27
  - description: Tasks and updates performed to maintain `backlog.md` and related tooling.
  - tasks:
    - ✅ Task 9015: Keep legend and add in-progress symbol
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Preserved "✅ = done, ☐ = open, ❌ = failed" and added "⏳ = in progress".

    - ✅ Task 9016: Renumber epics to zero-padded numeric IDs starting at 0000
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Converted epics A..H → 0000..0007 to avoid future letter limits.

    - ✅ Task 9017: Group tasks into Epics and mark epic status based on subtasks
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:

    - ✅ Task 9018: Add `## Epics - finished` and move fully completed epics there
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Epic 0000 moved to finished; removed duplication in main list.

    - ✅ Task 9019: Create `scripts/update_backlog.py` to validate and move finished epics
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Small conservative script added; package marker also created.

    - ☐ Task 0210: Auto-run the script after each interactive turn (automation)
      - status: rejected
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - User requested to disable automatic runs; marking this task as rejected per project decision.

    - ✅ Task 0211: Expand done-status synonyms and make detection robust (fuzzy/case-insensitive)
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:

    - ✅ Task 0212: Add unit tests for `update_backlog.py` and run in CI
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:

    - ☐ Task 0213: Add a git hook or CI job to run validation script before merging
      - status: reverted
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Added `.githooks/pre-commit` that runs the validator and fails the commit if it modifies the backlog; see `developer_rules.md` for install steps.

    - ✅ Task 0214: Implement automatic `updated:` metadata when task status changes
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:

    - ✅ Task 0215: Move `scripts` out of `.prompts` into repo root and update tests/docs
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:

    - ✅ Task 0216: Add `BACKLOG_MD` environment override for test safety
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Tests now set `BACKLOG_MD` to avoid overwriting canonical `backlog.md`.

    - ✅ Task 0217: Make cleanup and updater write atomically and accept `BACKLOG_MD`
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Use temp file + os.replace; prevent accidental deletion of backlog.

    - ✅ Task 0218: Re-add Task 0213 to backlog marked as reverted and expand statuses
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Added statuses `reverted`, `rejected`, and `cancelled` and updated legend/docs.

    - ✅ Task 0219: Fix tests that were accidentally corrupted during editing
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Corrected `tests/test_backlog_statuses.py` and ensured consistent indentation and use of `BACKLOG_MD`.

    - ✅ Task 0301: Update backlog tooling to recognize additional status semantics
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Extended `scripts/backlog_update.py` `validate()` and `move_finished_epics()` normalization maps to accept synonyms for `reverted`, `rejected`, and `cancelled` (including `revert`, `reject`, `aborted`, `cancel`).
        - Added `implemented` as a synonym for `done` to match existing backlog wording.
        - Ensured updater writes atomically and inserts `- updated: YYYY-MM-DD` when moving epics.
        - Ran full test suite: 142 passed.

    - ✅ Task 0220: Fix tuple-unpacking bug in `scripts/backlog_update.py` that caused updater to crash when diagnostic fields were present
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-27
      - notes:
        - Root cause: `moved_blocks` entries contained extra diagnostic fields (norms, status_lines) and the code attempted to unpack exactly three values, raising "too many values to unpack".
        - Verified by running targeted tests and the full suite: 142 passed.

    - ☐ Task 9022: Run Ruff and fix `/.vscode/tasks.json` (use Git Bash for tasks)
      - status: open
      - added: 2025-08-29
      - notes:

- ✅ Epic 0000: Core fixes done
  - status: done
  - added: 2025-08-25
  - closed: 2025-08-27
  - notes:

  - tasks:
    - ✅ Task 9030: Prevent overwriting master  on enable/disable; write changes to a managed file instead
      - status: done
      - added: 2025-08-29
      - closed: 2025-08-27
    - ✅ Task 9031: Make managed-file writes atomic; remove backup/rotation for YAML-managed files
      - status: done
      - added: 2025-08-26
      - closed: 2025-08-27
    - ✅ Task 0100: Add  to config schema and ensure / include it
      - status: done
      - added: 2025-08-26
      - closed: 2025-08-27
    - ✅ Task 0120: Move  block into an included managed file and add
      - status: done
      - added: 2025-08-26
      - closed: 2025-08-27
    - ✅ Task 0121: Simplify plugin discovery and add environment override for plugin dirs
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-28
    - ✅ Task 0122: Add unit test for  include merge behavior
      - status: done
      - added: 2025-08-26
      - closed: 2025-08-27
    - ✅ Task 0123: Document include pattern in  and
      - status: done
      - added: 2025-08-26
      - closed: 2025-08-27
    - ✅ Task 0124: Review and remove any remaining debug/logging cruft in plugin discovery
      - status: done
      - added: 2025-08-29
      - closed: 2025-08-29
    - ✅ Task 0500: Implement backup/undo CLI (in-progress)
      - status: done
      - added: 2025-08-29
      - closed: 2025-08-29
    - ✅ Task 0600: Make backlog status values and symbols configurable via YAML
      - status: done
      - added: 2025-08-29
      - closed: 2025-08-29
    - ✅ Task 0207: Add explicit per-role logging file config (logging.file_cli, logging.file_api)
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-28
    - ✅ Task 0302: Add retry/backoff for OpenAI 429 Too Many Requests
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-28
    - ✅ Task 0303: Clean up rate-limit logic
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-28
    - ✅ Task 0304: Limit messages length to the LLM
      - status: done
      - added: 2025-08-29
      - closed: 2025-08-29
    - ✅ Task 0305: Propagate  into LLM clients / OpenAI SDK
      - status: done
      - added: 2025-08-27
      - closed: 2025-08-28
    - ✅ Task 9020: Fix import-time corruption in src/agent_system/cli.py (fixed, closed: 2025-08-27)
      - status: done
      - added: 2025-08-30
      - closed: 2025-08-27
      - notes:

    - ✅ Task 9021: Ensure plugins info does not duplicate the NAME field (fixed, closed: 2025-08-27)
      - status: done
      - added: 2025-08-30
      - closed: 2025-08-27
      - notes:

## 3. Ideas

## 4. EOF

Note: This `backlog.md` must be updated after each interactive turn to reflect progress and new tasks.

---
EOF