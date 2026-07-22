Plugin contribution guidelines

- Put each plugin in its own folder under `plugins/<plugin_name>/`.
- Use `plugin.py` as the entrypoint file. Export exactly one symbol: `PLUGIN_FACTORY` (callable taking name, config, ssl_verify).
- Include `plugin.toml` with a `[plugin]` table: `name`, `description`, `version`, `entrypoint`.
- Declare the plugin's own pip requirements in `plugin.toml` (`[plugin] dependencies = [...]`); they are aggregated into the install by `scripts/aggregate_plugin_deps.py` (re-run it after changing deps). Keep them minimal.
- Put the plugin's tests in `plugins/<plugin_name>/tests/` (`test_*.py`); they inherit the shared fixtures from the root `conftest.py`.
- Prefer async `call()` implementations to match MCPServer interface.
- Activate and configure it in `config/plugins.yaml`
