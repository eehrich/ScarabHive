Plugin contribution guidelines

Verified quick reference: the Claude skill `.claude/skills/plugin-authoring/` (SKILL.md + references/).

- Put each plugin in its own folder under `src/plugins/<plugin_name>/` (or another plugin root `src/plugins_<name>/`). The folder name is the plugin type used in `type:` of a server entry.
- The entrypoint module sits directly in the plugin folder: `entrypoint = "plugin:PLUGIN_FACTORY"` (the default) or e.g. `"server:MyServer"`. The factory is called as `(name, system_config, server_config)`, plus `registry=` only if it carries `_accepts_registry = True` (agents: use `make_agent_plugin_factory`). Config-only plugins (`type = ["library"]`) have no entrypoint.
- Include `plugin.toml` with a `[plugin]` table: `name`, `description`, `version`, `requires = { agent_system = ">=0.6.0" }`, `type`, and `entrypoint` where needed. Check it with `python src/scripts/validate_plugin.py <plugin dir>`.
- Declare the plugin's own pip requirements in `plugin.toml` (`[plugin] dependencies = [...]`); they are aggregated into the install by `scripts/aggregate_plugin_deps.py` (re-run it after changing deps). Keep them minimal.
- Put the plugin's tests in `src/plugins/<plugin_name>/tests/` (`test_plugin_<plugin_name>_*.py`); they inherit the shared fixtures from the root `conftest.py`.
- Tool servers inherit from `SchemaBasedToolServer`: tools in `schema.yaml`, tool `{{ name }}_x` routes to `async def x(self, params)`. `server_config` is a pydantic model, not a dict — read it with `getattr`.
- Activate an instance under `plugins: servers:` (e.g. in `config/plugins.yaml`) with `enabled: true` (default `false`), and allow its tools in the agent's `agent_config.tools.allowed`.
