---
name: plugin-authoring
description: How to build or extend a ScarabHive plugin — plugin types (tool server, hooks, web, agent, library/config-only, LLM provider), plugin.toml, schema.yaml, the handler contract, status/cancellation, hooks and cache safety, agent YAMLs and enabling sub-agents in the SAM, the activation chain from plugin to a tool the agent sees, and tests. Load before creating or reworking a plugin, tool, hook, agent YAML or sub-agent under src/plugins*/.
---

# Building plugins

Everything in code, tool descriptions and commit messages is English. Agent prompts
are English too, unless a further plugin root (`src/plugins_<name>/`) documents an
exception of its own.

**Every statement here was checked against the code.** Long form: `docs/plugin_authoring.md`, `docs/plugin_hooks.md`,
`docs/_arch_plugin_architecture.md`. **When anything disagrees, the code wins.**

## Which type?

The runtime detects tool/hook/web capabilities from the object (tools from
`schema.yaml tools:`, hooks from `schema.yaml hooks:`, web via `get_web_router`).
`type` in plugin.toml is otherwise read only by `src/scripts/validate_plugin.py` —
**except `llm-provider`, which the LLM registry requires** (without it the provider
is silently not registered). Set it correctly anyway.

| You want … | Type | Base | Details |
|---|---|---|---|
| to give the model functions | `tool-server` | `SchemaBasedToolServer` | [tools.md](references/tools.md) |
| to intercept LLM calls / history | `hooks` | `SchemaBasedPluginHook` | [hooks.md](references/hooks.md) |
| both | `["tool-server","hooks"]` | `SchemaBasedHookToolServer` + `on_<hooktype>`, or a separate `hooks_plugin` | both |
| a panel / endpoints | `web` | `get_web_router` | skill `panel-authoring` |
| an agent without Python | `library` | just `agents/*.yaml` + `prompts/` + `skills/` | [agents.md](references/agents.md) |
| an agent with its own code | `tool-server` | `SchemaBasedAgent` + `make_agent_plugin_factory` | [agents.md](references/agents.md) |
| an LLM provider | `llm-provider`, and no `plugin.py` | `provider.py` exporting `PROVIDERS` | [agents.md](references/agents.md) §LLM |

**Check whether it already exists first** — about fifty plugins live in
`src/plugins/`. A second server entry with different config (`type: file_ops`)
often replaces a new plugin.

## Minimal layout (tool server)

```
src/plugins/my_plugin/
  plugin.toml      # required
  plugin.py        # entrypoint module, sits DIRECTLY in the plugin folder
  server.py
  schema.yaml      # required for tools; must sit next to the class's module
  README.md        # a short overview, see below
  my_plugin.guide  # the manual: panel, tools, hooks, settings
  tests/test_plugin_my_plugin_*.py
```

```toml
[plugin]
name = "my_plugin"
version = "0.1.0"
description = "One line."
entrypoint = "plugin:PLUGIN_FACTORY"   # default; "server:MyServer" works too
type = ["tool-server"]
category = "tools"
requires = { agent_system = ">=0.6.0" }  # required by the validator
dependencies = []                        # pip specs only
optional_dependencies = []               # pip specs an install may lack (requirements/optional.txt)
```

```python
# plugin.py
from .server import MyServer
PLUGIN_FACTORY = MyServer   # called as (name, system_config, server_config)
```

- **Plugin type = folder name**, not `name` from plugin.toml.
- No entrypoint file or no factory → folder skipped **at DEBUG only**. Import
  error → WARNING, the type is missing, every server entry using it logs
  "Unknown server type".
- New pip dependency: run `python scripts/aggregate_plugin_deps.py` afterwards
  (drift guard `tests/pluginsystem/test_plugin_deps_aggregation.py`). One that may
  fail to install (builds from source on some platforms) goes in
  `optional_dependencies`: `requirements/optional.txt`, installed best effort by
  the install scripts, not by `pip install -e .`. The code imports it lazily and
  answers with the fix when it is missing.

## Activation chain — when does an agent see the tool?

Every link must hold; almost every one fails **silently**:

1. The folder is in `plugins.plugin_dirs` (`src/plugins*` in `config/plugins.yaml`:
   `src/plugins` and every further `src/plugins_<name>` root).
2. An entry `plugins: servers: <instance>: {type: <folder name>, enabled: true}` —
   `enabled` defaults to **false** and is checked on the **raw** entry, not the
   inherited one. The entry may live in any included file (`config.yaml` includes
   `plugins.yaml`, `agents*/*.yaml`, `../src/plugins*/*/agents/*.yaml`). A top-level
   `agents:` key is silently ignored; a broken YAML file is skipped entirely.
3. The agent allows it in `agent_config.tools.allowed`. Empty = nothing allowed.
   Patterns: `instance/*`, `instance`, `instance/<full tool name>`, fnmatch.
   **The tool name carries the instance prefix:** `coder_fs/coder_fs_semantic_search`
   — `coder_fs/semantic_search` silently matches nothing.
4. A second instance (`workspace_file_ops`, `type: file_ops`, shipped disabled) has
   **different tool names** and needs its own allowlist entry.
5. Lists: without prefixes they replace the inherited list, `+x`/`!x` merge,
   mixing both → ValueError.

Check inheritance only via `get_tool_server_config` — the raw
`config.plugins.servers[name]` shows Pydantic defaults, not inheritance.

## Reading config

`server_config` is a Pydantic model with `extra="allow"`, **not a dict**:

```python
self.timeout = float(getattr(server_config, "timeout", 30))   # flat keys
cfg = getattr(server_config, "config", None) or {}              # nested config: block
```

- The framework **does not validate plugin config**. The `config:` block in
  `schema.yaml` is read only for hook plugins; for tool servers it is
  documentation — defaults belong in code.
- `${VAR}` is expanded from the environment / `config/secrets.env`; unset → `""` + WARNING.
- Hot reload: no watcher. `agent-cli reload` calls `reload_config(new_server_config)`
  only on servers that implement it. New servers need a restart — **the user does
  restarts.**
- If a config **model** changes (`src/agent_system/config/models.py`), update the
  JSON schema under `schemas/` too.
- **Never spell out a data path yourself.** The data directory can move
  (`AGENT_DATA_DIR`, else `paths.data_dir`). Default: `data_path("plugin", "x.db")`
  from `agent_system.paths`, called at run time; a value from the environment, the CLI or
  a DB row: `resolve_data_path(value)`. The loader moves `plugins.yaml` values and
  `schema.yaml` defaults under `data/` along. Guard: `tests/config/test_no_hardcoded_data_dir.py`.

## Rules that have already caused damage

- **Cache: every LLM request must be a prefix of the next.** Nothing ticking
  (clock, step counter) in the system prompt or at the front of history; never
  rewrite earlier messages. Guards: `tests/agent/test_agent_step_budget_note.py`,
  `tests/config/test_prompts_have_no_ticking_clock.py`. → [hooks.md](references/hooks.md)
- **`injected_by`:** every `role: user` message inserted by the loop or a plugin
  carries `injected_by="<plugin>"`. `injected_by is None` means "a person wrote
  this" — context_engineer, OKF, tool_preload, agent_continuation count turns by it.
- **The status end line names the result** (counts, ids), ≤140 chars, exactly one
  `end`/`error`. AST guard over all of `src/plugins`: `tests/plugins/test_status_end_lines.py`.
- **Validate arguments yourself** — the framework does not check tool arguments
  against the schema; `required`/`enum` are only hints to the model.
- **Return errors as `{"status": "error", "error": "<what to do>"}`.** A
  success-looking dict on failure is a silent failure.
- **Bound tool results yourself.** Only context_engineer (Pre-Layer T) caps them,
  and only for agents running that hook.
- **Destructive jobs (cleanup, TTL, sweep) are opt-in** — default off, in
  `schema.yaml` AND in code. Tests without operator config hit the real `data/` otherwise.
- **A new sub-agent must be enabled in the SAM, or it cannot be spawned** →
  [agents.md](references/agents.md) §SAM.
- **Prompts are read from disk on every render** — an edit takes effect
  immediately, including in runs already in flight.
- **No provider tables** (`if provider == "google"`, alias dicts) in LLM plugins.

## Validate before testing

Scripts in `src/scripts/` (run with `.venv/Scripts/python.exe`; all read-only unless noted):

| Script | Checks | Run |
|---|---|---|
| `validate_plugin.py` | plugin.toml against `schemas/plugin-config.schema.json` (e.g. missing `requires`), schema.yaml sections per `type`, tool definitions, file layout, hooks, template vars | `validate_plugin.py src/plugins/my_plugin` · `--plugin my_plugin` · `--all` |
| `validate_all_tool_schemas.py` | every tool in every schema.yaml is valid OpenAI/MCP format **and routes to a method the plugin defines**; templated schemas rendered in both states | `validate_all_tool_schemas.py` · `--plugin my_plugin` |
| `validate_agent_configs.py` | agent YAML: syntax, `plugins.servers` shape, `tools`/`hooks` at the right level, Pydantic models (`agent_config` typos, `self_tool_descriptions` in the wrong place) | `validate_agent_configs.py src/plugins/my_plugin/agents/*.yaml` (also `validate-agents`) |
| `analyze_plugin_config.py` | lists the config keys the code reads (`getattr(server_config, …)`) | `analyze_plugin_config.py src/plugins/my_plugin` |
| `generate_config_schemas.py` | **writes** `schemas/*.schema.json` from the config models — run after changing `config/models.py`; drift test `tests/config/test_config_schemas.py` | `generate_config_schemas.py` |

`validate_plugin.py --merge-config` **writes** missing config keys into schema.yaml.

⚠️ Measured: the validators do **not** catch the silent failures of the activation
chain — a dead allowlist pattern (`coder_fs/semantic_search`), a sub-agent missing
from `allowed_agents`. (A `hooks.overrides` key matching no hook is logged as a
warning at startup, not by the validators.) Check those with
`load_settings()` + `get_tool_server_config` in a config test
(`src/plugins/research/tests/test_research_config.py`).

## README and guide

The README is a short overview: what the plugin is for, its tools, hooks and panel in a
line each, how to switch it on -- and that the details are in its guide. The guide
(`<folder>.guide`, see below) is the manual: the panel for users with a screenshot
(`src/scripts/guide_screenshots.py` shoots it from the panel test's stub app, never from
real data), every tool with its parameters and answers, every hook with its settings, the
server settings. Write it from the code, not from the old README -- those drift.

For a plugin whose tools a model calls, the guide also carries the "Model Experience"
(a plugin without a guide yet keeps it in its README; not enforced by a test — write it
anyway). Three parts:

1. **What the model sees** — tool descriptions, error strings, injected notices, verbatim.
2. **Token and cache effect** — append-only / prefix-changing (when, how often) / none.
3. **Known gaps** — deliberate limits with the reason.

Example of the new form: `src/plugins/todo/todo.guide` (with its short README). Still in the old
form, the parts in the README: `media_ops`, `agent_watchdog`. A security-heavy example: `src/plugins/terminal/terminal.guide`.

## User documentation: the Help panel

The Help panel shows a plugin's docs next to the manual, found by convention:
`<plugin folder>/<folder name>.guide` (AmigaGuide, extended by headings, lists, tables and
code blocks in its own syntax -- not Markdown), else the plugin's `README.md` rendered as
Markdown. A guide button or a README link to `docs/*.md` opens that file in the viewer. With
either, the plugin's panel gets a help button in the shell by itself (catalogue entry `help`;
for an instance of another name the plugin type the loader resolves for it counts). In the plugin's own panel: `<pk-guide guide="<folder>" node="config">` from
`/static/kit/guide.js`. Format and rules: Help → "Writing a guide", long form
`docs/help_amigaguide.md`. `tests/ui/test_help.py -k repository` checks every guide in
the repo (dead links, unknown commands).

## Tests

- Next to the plugin: `src/plugins/<name>/tests/test_plugin_<name>_*.py`.
  `tests/plugins/` is for cross-plugin tests only. Basenames unique repo-wide.
- `pytest.ini`: `filterwarnings=error`, `asyncio_mode=auto`, 120 s timeout. The root
  `conftest.py` replaces `build_client` with a fake — no real LLM calls.
- Test through the real path: tools via `call_with_status` (otherwise `_status` is
  `None`), config via `load_settings()` + `get_tool_server_config`.
  Config-only example: `src/plugins/research/tests/test_research_config.py`.
- Caches/storage on `tmp_path` (`PluginCache` writes to `data/cache` otherwise).
- **Mutation-check every new test:** break the production line, the test must go
  red. Craft: skill `unit-testing`.
- Test selectively, never the whole suite without reason. **Don't run pytest on the
  server:** a test that starts the app as a subprocess loads the host's real config
  and starts its enabled plugins (`tests/app/test_app_shutdown.py`).
  The root conftest no longer kills what it did not start — only processes carrying
  its session's `AGENT_SYSTEM_TEST_SESSION` marker, plus those of sessions that died.
  A child you start with an environment of its own (an allowlist) needs the marker
  passed on, or its leftovers stay. **A test that starts a pytest of its own sets
  `AGENT_SYSTEM_TEST_NO_REAP=1` for it** — use `run_nested_pytest` in
  `tests/other/test_conftest_process_cleanup.py`; the conftest cannot detect a nested run.

## Don't forget

- CLI (`cli.py`, `__main__.py`, pyproject script) is **optional**, despite the docs.
- Web UI/panel: skill `panel-authoring`. Slash commands: `commands:` in
  schema.yaml → [tools.md](references/tools.md).
- A plugin in a further root (`src/plugins_<name>/`): load that root's own skill first, if it has one.
