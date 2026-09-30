# Contributing a plugin

The checklist for a plugin in `src/plugins/`. The full guide is
[docs/plugin_authoring.md](../../docs/plugin_authoring.md), hooks are in
[docs/plugin_hooks.md](../../docs/plugin_hooks.md), and the general rules (setup, tests, lint,
commit messages) in the [root CONTRIBUTING.md](../../CONTRIBUTING.md).
[example/](example/) is a small plugin written the way this list asks.

Working with Claude Code (or another agent that reads skills)? Load the skills first -- they
are checked against the code and carry the rules that have already caused damage:
[plugin-authoring](../../.claude/skills/plugin-authoring/SKILL.md) for plugins, tools, hooks,
agents and the guide ([references/](../../.claude/skills/plugin-authoring/references/)), and
[panel-authoring](../../.claude/skills/panel-authoring/SKILL.md) for a panel in the UI.

## Layout

- One folder per plugin: `src/plugins/<name>/`. The folder name is the plugin type that a
  server entry names in `type:`.
- `plugin.toml` with a `[plugin]` table: `name`, `version`, `description`, `type`
  (`tool-server`, `hooks`, `web`, `library`, `llm-provider`), `requires = { agent_system =
  ">=0.6.0" }`, and `entrypoint` where there is code (default `plugin:PLUGIN_FACTORY`).
- The entrypoint module sits directly in the folder. The factory is called as
  `(name, system_config, server_config)`; `server_config` is a pydantic model, read it with
  `getattr`, not as a dict.
- Pip requirements go in `plugin.toml` (`dependencies = [...]`); run
  `python scripts/aggregate_plugin_deps.py` afterwards. Keep them few.
- Check the manifest and the tool schemas:
  `python src/scripts/validate_plugin.py src/plugins/<name>` and
  `python src/scripts/validate_all_tool_schemas.py --plugin <name>`.

## Tools

- A tool server inherits `SchemaBasedToolServer`. Tools are declared in `schema.yaml`; the
  tool `{{ name }}_x` runs `async def x(self, params)`.
- The tool and parameter descriptions are read by the model on every call and cost tokens
  each time: say what the model needs to call the tool well, nothing more. Everything else
  goes into the guide.
- The framework does not check arguments against the schema: validate them yourself.
- Errors are `{"status": "error", "error": "<what to do>"}` -- never an answer that looks
  like success.
- Bound what a tool returns; nothing does it for you.
- The chat's status line ends with exactly one `end` or `error` that names the result
  (counts, ids), at most 140 characters (`tests/plugins/test_status_end_lines.py`).
- A message a plugin inserts into the history carries `injected_by="<plugin>"`, and nothing
  that changes on every call (a clock, a counter) goes into the system prompt: every request
  must be a prefix of the next, or the prompt cache breaks.
- A job that deletes or cleans up is off by default.

## Documentation

- The manual is `<name>.guide` next to `plugin.toml` (AmigaGuide, English), shown in the
  Help panel: the panel for the person using it (with a screenshot from the panel test's
  stub app, `src/scripts/guide_screenshots.py`), every tool and hook with its parameters as
  the code applies them, what the model sees, and how to set it up. Write it from the code.
  `tests/ui/test_help.py -k repository` checks every guide.
- The `README.md` is a short overview that points to the guide.

## Tests

- Next to the plugin: `src/plugins/<name>/tests/test_plugin_<name>_*.py`. Test through the
  real path (`call_with_status` for tools, `load_settings()` + `get_tool_server_config` for
  configuration). Caches and files go to `tmp_path`.
- No real LLM, network or paid API calls.
- Break the code a new test covers once: the test must go red. One that stays green checks
  nothing.

## Switching it on

- A server entry under `plugins: servers:` (in `config/plugins.yaml` or an agent's YAML) with
  `type: <name>` and `enabled: true` -- the default is off.
- An agent gets the tools through its allowlist, `agent_config.tools.allowed`, for example
  `+<instance>/*`. Tool names carry the instance's name as a prefix.
- A plugin that ships a sub-agent registers it in the sub-agent manager's `allowed_agents`,
  or it cannot be started.
