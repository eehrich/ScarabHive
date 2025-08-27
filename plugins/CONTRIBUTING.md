Plugin contribution guidelines

- Put each plugin in its own folder under `plugins/<plugin_name>/`.
- Use `plugin.py` as the entrypoint file. Export either `register()` or `PLUGIN_NAME`/`PLUGIN_FACTORY`.
- Include `plugin.yaml` with metadata: `name`, `description`, `version`.
- Keep plugin dependencies minimal and document them in `plugin.yaml` if needed.
- Prefer async `call()` implementations to match MCPServer interface.
